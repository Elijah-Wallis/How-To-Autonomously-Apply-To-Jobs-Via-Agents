#!/usr/bin/env python3
"""Orchestrator client (Phase 2): Search -> Identify -> Apply workflow."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

try:
    from mcp.client.stdio import StdioServerParameters, stdio_client
except Exception:  # pragma: no cover - sdk fallback
    from mcp.client.stdio import stdio_client

    StdioServerParameters = None  # type: ignore

try:
    from mcp import ClientSession
except Exception:  # pragma: no cover - sdk fallback
    from mcp.client.session import ClientSession


SERVER_SCRIPT = Path(__file__).resolve().parent / "server.py"
PROFILE_PATH = Path(__file__).resolve().parent / "data/profile.json"
REPORT_PATH = Path(__file__).resolve().parent / "data/targets.json"
STATE_PATH = Path(__file__).resolve().parent / "data/auth_state.json"
DEBUG_SEARCH_SCREENSHOT = Path(__file__).resolve().parent / "data/debug_search_results.png"
DEBUG_PAGE_HTML = Path(__file__).resolve().parent / "data/debug_page.html"
SEARCH_TERM = "Warehouse"
LOCATION_TERM = "Plano, TX"
TARGETS_LIMIT = 10
HEADLESS_DEFAULT = False
APPLE_ID = "elijahcwallis@gmail.com"
APPLE_PASS = "Belinda0301!"
AUTO_SUBMIT = os.getenv("AUTO_SUBMIT", "0") == "1"
PYTHON_EXECUTABLE = os.environ.get("MCP_SERVER_PYTHON", sys.executable)

APPLE_LOGIN_URL = "https://secure.indeed.com/account/login"
APPLE_SIGNIN_BUTTON_CANDIDATES = [
    "button#apple-signin-button",
    "button:has-text('Continue with Apple')",
    "a:has-text('Continue with Apple')",
    "[data-provider='apple']",
]
APPLE_ID_SELECTORS = [
    "#account_name",
    "input[name='account_name']",
    "input[name='username']",
    "input[name='email']",
    "input[type='email']",
]
APPLE_PASSWORD_SELECTORS = [
    "input[type='password']",
    "#password",
    "input[name='password']",
]
APPLE_2FA_SELECTORS = [
    "input[autocomplete='one-time-code']",
    "input[name='verificationCode']",
    "input[name='code']",
    "input[maxlength='6']",
]
APPLE_NEXT_BUTTON_CANDIDATES = [
    "button:has-text('Continue')",
    "button:has-text('Next')",
    "button[type='submit']",
    "input[type='submit']",
]
APPLE_VERIFY_BUTTON_CANDIDATES = [
    "button:has-text('Verify')",
    "button:has-text('Trust')",
    "button:has-text('Continue')",
    "button[type='submit']",
    "input[type='submit']",
]


SEARCH_WHAT_CANDIDATES = [
    "input[aria-label='What']",
    "input[placeholder='Job title, keywords, or company']",
    "input[placeholder='What']",
    "input#what",
    "input[data-testid='text-input-what']",
    "#text-input-what",
    "input[name='q']",
    "input#text-input-what",
]

SEARCH_WHERE_CANDIDATES = [
    "input[aria-label='Where']",
    "input[placeholder='City, state, zip code, or remote']",
    "input[placeholder='City, state, or zip code']",
    "input[placeholder='Where']",
    "input#where",
    "input[data-testid='text-input-where']",
    "#text-input-where",
    "input[name='l']",
]

SEARCH_BUTTON_CANDIDATES = [
    "button:has-text('Find jobs')",
    "button:has-text('Search')",
    "button[aria-label='Search']",
    "[data-testid='jobsearchButton']",
    "button[type='submit'][data-testid='what-where-search-button']",
    "button[type='submit']",
]

REDIRECT_PATTERNS = [
    "workday",
    "taleo",
    "icims",
    "greenhouse",
    "jobvite",
    "smartrecruiters",
    "bamboohr",
]

REDIRECT_TEXT_PATTERNS = [
    r"apply on company site",
    r"create an? account",
    r"sign in to continue",
    r"already have an account",
]

INDEED_ALLOWED_HOST_SUFFIX = "indeed.com"
INDEED_ALLOWED_TARGET_PATH = "/viewjob"
INDEED_CANONICALIZE_PATHS = {"/rc/clk", "/pagead/clk", "/clk", "/m/rc/clk", "/m/pagead/clk"}

# Explicit Playwright selector candidates for Apply controls on Indeed /viewjob pages.
# Used before falling back to heuristic DOM scanning in _find_apply_selector.
INDEED_APPLY_SELECTOR_CANDIDATES = [
    "#indeedApplyButton",
    "#applyButtonLink",
    "a#applyButtonLink",
    "button[data-tn-element='applyButton']",
    "[data-tn-element='applyButton']",
    "[data-testid='indeedApplyButton']",
    "[data-testid='applyButton']",
    "[data-testid='viewJobApplyButton']",
    "[data-testid='applyButtonLink']",
    "button[aria-label*='Apply' i]",
    "a[aria-label*='Apply' i]",
    "button:has-text('Easy Apply')",
    "button:has-text('Apply now')",
    "button:has-text('Apply Now')",
    # Keep the generic text selectors last to reduce false positives.
    "button:has-text('Apply')",
    "a:has-text('Easy Apply')",
    "a:has-text('Apply now')",
    "a:has-text('Apply Now')",
    "a:has-text('Apply')",
]

FIELD_PATTERNS = {
    "first_name": [r"\bfirst\s+name\b", r"\bgiven\s+name\b", r"\bfirst\b"],
    "last_name": [r"\blast\s+name\b", r"\bsurname\b"],
    "full_name": [r"\bfull\s+name\b", r"\byour\s+name\b"],
    "email": [r"\bemail\b", r"\be[- ]?mail\b", r"\bwork\s+email\b"],
    "phone": [r"\bphone\b", r"\bmobile\b", r"\bcontact\s+number\b", r"\btelephone\b"],
    "resume": [r"\bresume\b", r"\bcv\b", r"\bcurriculum\s+vitae\b", r"\bupload\b"],
}


class OrchestratorError(RuntimeError):
    """Raised on recoverable orchestration-level tool failures."""


def _log(message: str) -> None:
    print(f"[orchestrator] {message}")


def _human_delay(min_seconds: float = 0.8, max_seconds: float = 1.8) -> float:
    return random.uniform(min_seconds, max_seconds)


def _parse_json_if_possible(payload: Any) -> Any:
    if isinstance(payload, (dict, list)):
        return payload
    if not isinstance(payload, str):
        return None
    stripped = payload.strip()
    if not stripped:
        return None
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            return json.loads(stripped)
        except Exception:
            return None
    return None


def _extract_tool_text(result: Any) -> str:
    if isinstance(result, str):
        return result.strip()

    content = None
    if hasattr(result, "content"):
        content = getattr(result, "content")
    elif isinstance(result, dict):
        content = result.get("content")

    if content is None and hasattr(result, "text"):
        text = getattr(result, "text")
        return str(text).strip()

    pieces = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                pieces.append(block)
            elif isinstance(block, dict) and "text" in block:
                pieces.append(str(block["text"]))
            elif hasattr(block, "text"):
                pieces.append(str(getattr(block, "text")))

    if pieces:
        return "\n".join(piece for piece in pieces if isinstance(piece, str)).strip()

    if isinstance(result, dict) and ("result" in result or "error" in result):
        result_value = result.get("error", result.get("result"))
        return str(result_value)

    return ""


def _tool_errored(result: Any, message: str) -> bool:
    if getattr(result, "is_error", False) or getattr(result, "isError", False):
        return True
    lower = (message or "").lower()
    return any(
        marker in lower
        for marker in [
            "not found",
            "not visible",
            "browser is not",
            "failed",
            "failed to",
            "timeout",
            "error",
            "exception",
            "crash",
            "timeout",
        ]
    )


async def _call_tool(session: ClientSession, name: str, arguments: dict[str, Any] | None = None) -> str:
    args = arguments or {}
    try:
        result = await session.call_tool(name=name, arguments=args)
    except TypeError:
        result = await session.call_tool(name, args)  # fallback for older signatures

    message = _extract_tool_text(result)
    if _tool_errored(result, message):
        raise OrchestratorError(f"[{name}] {message or 'tool execution error'}")
    return message


async def _call_evaluate_json(session: ClientSession, script: str) -> Any:
    message = await _call_tool(session, "browser_evaluate", {"script": script})
    parsed = _parse_json_if_possible(message)
    if parsed is not None:
        return parsed
    return message


def _is_redirect_or_ats(url: str, body_text: str) -> bool:
    lower_url = (url or "").lower()
    lower_body = (body_text or "").lower()

    for token in REDIRECT_PATTERNS:
        if token in lower_url or token in lower_body:
            return True

    for token in REDIRECT_TEXT_PATTERNS:
        if re.search(token, lower_body):
            return True
    return False


def _canonicalize_indeed_target_url(url: str) -> str | None:
    """Strict allowlist: only target Indeed /viewjob pages (canonicalized via jk=)."""
    raw = (url or "").strip()
    if not raw:
        return None

    try:
        parsed = urlparse(raw)
    except Exception:
        return None

    scheme = (parsed.scheme or "").lower()
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""
    if scheme not in {"http", "https"}:
        return None
    if not host:
        return None
    if not (host == INDEED_ALLOWED_HOST_SUFFIX or host.endswith("." + INDEED_ALLOWED_HOST_SUFFIX)):
        return None

    qs = parse_qs(parsed.query or "")
    jk = ""
    if "jk" in qs and qs["jk"]:
        jk = str(qs["jk"][0]).strip()
    if not jk:
        return None

    # Canonicalize click-tracking endpoints to stable /viewjob.
    if path in INDEED_CANONICALIZE_PATHS:
        path = INDEED_ALLOWED_TARGET_PATH

    if path != INDEED_ALLOWED_TARGET_PATH:
        return None

    return urlunparse(
        (
            "https",
            "www." + INDEED_ALLOWED_HOST_SUFFIX,
            INDEED_ALLOWED_TARGET_PATH,
            "",
            urlencode({"jk": jk}),
            "",
        )
    )


def _resolve_profile_file_path(profile_path: Path, value: str | None, default_name: str) -> Path:
    value = str(value or "").strip()
    candidate = Path(default_name if not value else value)

    if candidate.is_absolute():
        return candidate

    normalized = candidate.as_posix().lstrip("./")
    project_root = profile_path.parent.parent
    if normalized.startswith("data/") or normalized.startswith("data\\"):
        return (project_root / normalized).resolve()
    return (profile_path.parent / candidate).resolve()


def _write_dummy_pdf(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"%PDF-1.4\n%Dummy PDF\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\nxref\n0 3\n0000000000 65535 f \n"
        b"0000000009 00000 n \n0000000079 00000 n \ntrailer\n<< /Size 3 /Root 1 0 R >>\nstartxref\n173\n%%EOF\n"
    )


def _ensure_profile_exists(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            raw = None
        if isinstance(raw, dict):
            return

    default_profile = {
        "first_name": "Elijah",
        "last_name": "Wallis",
        "email": "elijahcwallis@gmail.com",
        "phone": "985-991-4360",
        "address_line1": "3201 Wynwood Dr",
        "city": "Plano",
        "state": "TX",
        "zip": "75074",
        "location": "Plano",
        "resume_path": "data/resume.pdf",
    }
    path.write_text(json.dumps(default_profile, indent=2), encoding="utf-8")
    _log("Generated default profile.")


def _ensure_resume_artifact(profile_path: Path) -> None:
    if not profile_path.exists():
        return

    try:
        raw = json.loads(profile_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}

    resume_key = "data/resume.pdf"
    for key in ("resume_path", "resume", "cv_path", "cv"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            resume_key = value.strip()
            break

    resume_path = _resolve_profile_file_path(profile_path, resume_key, default_name="resume.pdf")
    _write_dummy_pdf(resume_path)


def _normalize_profile(profile_path: Path) -> dict[str, str]:
    if not profile_path.exists():
        raise FileNotFoundError(f"Profile file not found: {profile_path}")

    raw = json.loads(profile_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Profile JSON must be an object with profile fields.")

    norm = {
        str(k).strip().lower().replace(" ", "_"): str(v).strip()
        for k, v in raw.items()
        if isinstance(v, (str, int, float))
    }

    full_name = norm.get("name", "") or norm.get("full_name", "")
    first_name = norm.get("first_name", "")
    last_name = norm.get("last_name", "")

    if not first_name and full_name:
        parts = [part for part in full_name.split() if part]
        if parts:
            first_name = parts[0]
            if len(parts) > 1:
                last_name = parts[-1]

    resume_candidates = [norm.get("resume"), norm.get("resume_path"), norm.get("cv"), norm.get("cv_path")]
    resume_path = _resolve_profile_file_path(
        profile_path,
        next((item for item in resume_candidates if item), None),
        default_name="resume.pdf",
    )
    if not resume_path.exists():
        _write_dummy_pdf(resume_path)
        _log(f"Resume file not found. Created dummy file: {resume_path}")

    return {
        "full_name": full_name,
        "first_name": first_name,
        "last_name": last_name,
        "email": norm.get("email", ""),
        "phone": norm.get("phone", ""),
        "resume": str(resume_path),
        "resume_path": str(resume_path),
    }


def _extract_links_from_text(raw: str, limit: int = TARGETS_LIMIT) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    seen = set()

    for match in re.finditer(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", raw or ""):
        title = re.sub(r"\s+", " ", match.group(1)).strip()
        url = match.group(2).strip().rstrip(").,")
        if not title:
            continue
        canonical = _canonicalize_indeed_target_url(url)
        if not canonical:
            continue
        key = canonical.lower()
        if key in seen:
            continue
        seen.add(key)
        links.append({"title": title, "link": canonical})
        if len(links) >= limit:
            return links

    if len(links) >= limit:
        return links

    for match in re.finditer(r"https?://[^\s\]\)]+", raw or ""):
        url = match.group(0).rstrip(").,")
        canonical = _canonicalize_indeed_target_url(url)
        if not canonical:
            continue
        key = canonical.lower()
        if key in seen:
            continue
        seen.add(key)
        links.append({"title": "(title unavailable)", "link": canonical})
        if len(links) >= limit:
            break

    return links[:limit]


def _infer_field_key(descriptor: dict[str, Any]) -> str | None:
    if descriptor.get("type", "").lower() == "file":
        return "resume"

    haystack = " ".join(
        str(descriptor.get(k, "")).lower()
        for k in ("name", "id", "placeholder", "ariaLabel", "label", "tag", "type")
    )
    haystack = re.sub(r"\s+", " ", haystack)

    for key, patterns in FIELD_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, haystack, flags=re.I):
                return key

    return None


def _map_fields_to_profile(
    profile: dict[str, str],
    fields: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    used: set[str] = set()
    mapped: list[dict[str, Any]] = []
    ordered_keys = ["first_name", "last_name", "full_name", "email", "phone", "resume"]

    for key in ordered_keys:
        for descriptor in fields:
            selector = str(descriptor.get("selector", "")).strip()
            if not selector or selector in used:
                continue
            if _infer_field_key(descriptor) != key:
                continue
            value = profile.get(key, "")
            if not value:
                continue
            used.add(selector)
            mapped.append({"field": key, "selector": selector, "value": value, "type": descriptor.get("type", "")})
            break

    return mapped


async def _type_into_fields(session: ClientSession, mapped_fields: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    filled: list[str] = []
    skipped: list[str] = []

    for item in mapped_fields:
        field = item["field"]
        selector = item["selector"]
        value = item["value"]
        field_type = str(item.get("type", "")).lower()

        if field == "resume" or field_type == "file":
            if not value:
                skipped.append("resume")
                continue

            await _call_tool(session, "browser_upload", {"selector": selector, "file_path": value})
            filled.append("resume")
            await asyncio.sleep(_human_delay(0.3, 0.8))
            continue

        await _call_tool(session, "browser_type", {"selector": selector, "text": value, "delay_ms": 50})
        filled.append(field)
        await asyncio.sleep(_human_delay(0.2, 0.6))

    return filled, skipped


async def _find_apply_selector(session: ClientSession) -> str | None:
    script = """
    (() => {
      const candidates = Array.from(document.querySelectorAll('button, a, input[type="submit"], input[type="button"], [role="button"]'));

      const selectorFor = (el) => {
        if (!el || !el.isConnected) return '';
        if (el.id) return `#${CSS.escape(el.id)}`;
        const testid = (el.getAttribute && el.getAttribute('data-testid')) ? (el.getAttribute('data-testid') || '').trim() : '';
        if (testid) return `[data-testid="${CSS.escape(testid)}"]`;

        const parts = [];
        let current = el;
        let depth = 0;
        while (current && current.nodeType === 1 && current !== document.body && depth < 8) {
          let segment = current.tagName.toLowerCase();
          const parent = current.parentElement;
          if (!parent) { parts.unshift(segment); break; }
          let index = 1;
          let sibling = current.previousElementSibling;
          while (sibling) {
            if (sibling.tagName === current.tagName) index += 1;
            sibling = sibling.previousElementSibling;
          }
          segment += `:nth-of-type(${index})`;
          parts.unshift(segment);
          current = parent;
          depth += 1;
        }
        return parts.join(' > ');
      };

      const textFor = (el) => {
        return [
          el.textContent || '',
          el.value || '',
          el.getAttribute('aria-label') || '',
          el.getAttribute('title') || '',
        ].join(' ').toLowerCase();
      };

      const isVisible = (el) => {
        if (!el) return false;
        const style = window.getComputedStyle(el);
        if (!style) return false;
        if (style.display === 'none' || style.visibility === 'hidden' || parseFloat(style.opacity || '1') === 0) return false;
        const r = el.getBoundingClientRect();
        if (!r) return false;
        return r.width > 2 && r.height > 2;
      };

      const score = (el) => {
        const t = textFor(el).replace(/\\s+/g, ' ').trim();
        if (!t) return -999;
        // Reduce false positives from filter UIs.
        if (t.includes('apply filters') || t.includes('applied filters') || t.includes('filter')) return -50;

        let s = 0;
        if (t.includes('easy apply') || t.includes('quick apply')) s += 60;
        if (t.includes('apply now')) s += 40;
        if (t === 'apply' || t.startsWith('apply ')) s += 25;

        const tag = (el.tagName || '').toLowerCase();
        if (tag === 'button') s += 8;
        if (tag === 'a') s += 2;
        if (el.id && /apply/i.test(el.id)) s += 10;
        const testid = (el.getAttribute('data-testid') || '');
        if (/apply/i.test(testid)) s += 12;
        const aria = (el.getAttribute('aria-label') || '');
        if (/apply/i.test(aria)) s += 8;

        // Prefer controls nearer the top (primary CTA area) without overfitting.
        const r = el.getBoundingClientRect();
        if (r && isFinite(r.top)) s += Math.max(0, 10 - Math.min(10, Math.floor(Math.abs(r.top) / 120)));
        return s;
      };

      let best = null;
      let bestScore = -999;
      for (const el of candidates) {
        if (!isVisible(el)) continue;
        const s = score(el);
        if (s > bestScore) { bestScore = s; best = el; }
      }
      if (!best || bestScore < 10) return JSON.stringify({ found: false });
      return JSON.stringify({ found: true, selector: selectorFor(best), score: bestScore });
    })();
    """

    payload = await _call_evaluate_json(session, script)
    if isinstance(payload, dict) and payload.get("found"):
        selector = payload.get("selector")
        if selector:
            return str(selector)
    return None


async def _click_apply_control(session: ClientSession) -> str | None:
    """Click the most likely Apply control; returns the selector that was clicked (or None)."""
    last_error: Exception | None = None
    # 1) Try explicit Playwright selectors first (robust across Indeed variants).
    for selector in INDEED_APPLY_SELECTOR_CANDIDATES:
        try:
            await _call_tool(session, "browser_click", {"selector": selector})
            return selector
        except OrchestratorError as exc:
            last_error = exc
            continue

    # 2) Heuristic DOM scan -> click CSS selector.
    apply_selector = await _find_apply_selector(session)
    if isinstance(apply_selector, str) and apply_selector.strip():
        try:
            await _call_tool(session, "browser_click", {"selector": apply_selector})
            return apply_selector
        except OrchestratorError as exc:
            last_error = exc

    if last_error is not None:
        _log(f"Apply click failed after all candidates: {last_error}")
    return None


async def _find_submit_selector(session: ClientSession) -> str | None:
    script = """
    (() => {
      const candidates = Array.from(document.querySelectorAll('button, a, input[type=\"submit\"], input[type=\"button\"], [role=\"button\"]'));
      const selectorFor = (el) => {
        if (!el || !el.isConnected) return '';
        if (el.id) return `#${CSS.escape(el.id)}`;

        const parts = [];
        let current = el;
        let depth = 0;
        while (current && current.nodeType === 1 && current !== document.body && depth < 10) {
          let segment = current.tagName.toLowerCase();
          const parent = current.parentElement;
          if (!parent) { parts.unshift(segment); break; }
          let index = 1;
          let sibling = current.previousElementSibling;
          while (sibling) {
            if (sibling.tagName === current.tagName) index += 1;
            sibling = sibling.previousElementSibling;
          }
          segment += `:nth-of-type(${index})`;
          parts.unshift(segment);
          current = parent;
          depth += 1;
        }
        return parts.join(' > ');
      };

      const score = (el) => {
        const hay = [
          el.textContent || '',
          el.value || '',
          el.getAttribute('aria-label') || '',
          el.getAttribute('title') || '',
        ].join(' ').toLowerCase();
        if (!hay.trim()) return 0;
        if (hay.includes('submit') || hay.includes('continue') || hay.includes('save')) return 3;
        if (hay.includes('apply') || hay.includes('next') || hay.includes('finish')) return 2;
        return 1;
      };

      let best = null;
      let bestScore = -1;
      for (const el of candidates) {
        const s = score(el);
        if (s > bestScore) {
          bestScore = s;
          best = el;
        }
      }
      if (!best) return JSON.stringify({found: false});
      return JSON.stringify({found: true, selector: selectorFor(best), score: bestScore});
    })();
    """
    payload = await _call_evaluate_json(session, script)
    if isinstance(payload, dict) and payload.get("found"):
        selector = payload.get("selector")
        if selector:
            return str(selector)
    return None


async def _wait_for_selector(
    session: ClientSession,
    selectors: list[str],
    *,
    timeout_seconds: float = 20.0,
    poll_seconds: float = 0.6,
    require_visible: bool = True,
) -> str | None:
    script = """
    (() => {
      const selectors = __SELECTORS__;
      const requireVisible = __REQUIRE_VISIBLE__;
      const isVisible = (el) => {
        if (!el) return false;
        const style = window.getComputedStyle(el);
        if (!style) return false;
        if (requireVisible && (style.display === 'none' || style.visibility === 'hidden' || parseFloat(style.opacity || '1') === 0)) {
          return false;
        }
        const rect = el.getClientRects && el.getClientRects().length;
        if (requireVisible && rect === 0) return false;
        return true;
      };

      for (const selector of selectors) {
        try {
          const el = document.querySelector(selector);
          if (isVisible(el)) return selector;
        } catch (err) {
          continue;
        }
      }
      return null;
    })();
    """
    loop_deadline = asyncio.get_running_loop().time() + timeout_seconds
    payload_selectors = json.dumps(selectors)
    final_script = script.replace("__SELECTORS__", payload_selectors).replace("__REQUIRE_VISIBLE__", str(require_visible).lower())

    while asyncio.get_running_loop().time() < loop_deadline:
        found = await _call_evaluate_json(session, final_script)
        if isinstance(found, str):
            if found.lower() == "null":
                found = None
            else:
                return found
        elif found is not None:
            if isinstance(found, list) and found:
                return str(found[0])
            if isinstance(found, (str, int)):
                return str(found)

        await asyncio.sleep(poll_seconds)

    return None


async def _inspect_auth_state(session: ClientSession) -> dict[str, Any]:
    script = r"""
    (() => {
      const url = window.location.href || '';
      const hasAvatar = !!document.querySelector('[aria-label*="Account"], [aria-label*="profile"], [data-testid*="account"], img[alt*="avatar"]');
      return JSON.stringify({ url, hasAvatar });
    })();
    """

    signature = await _call_evaluate_json(session, script)
    if isinstance(signature, dict):
        return {"url": str(signature.get("url", "")), "has_avatar": bool(signature.get("hasAvatar"))}
    return {"url": "", "has_avatar": False}


async def _is_logged_in(session: ClientSession) -> bool:
    signature = await _inspect_auth_state(session)
    url = signature.get("url", "").lower()
    if "secure.indeed.com/account/login" not in url and "indeed.com/account/login" not in url and "indeed.com" in url:
        return True
    return bool(signature.get("has_avatar"))


async def _perform_login(session: ClientSession) -> None:
    _log("Performing automated Apple login flow.")
    await _call_tool(session, "browser_navigate", {"url": APPLE_LOGIN_URL})
    await asyncio.sleep(_human_delay(1.0, 1.8))

    await _click_with_fallback(session, APPLE_SIGNIN_BUTTON_CANDIDATES)
    await asyncio.sleep(_human_delay(1.0, 1.8))

    email_selector = await _wait_for_selector(session, APPLE_ID_SELECTORS, timeout_seconds=25.0)
    if not email_selector:
        raise OrchestratorError("Unable to find Apple ID field.")

    await _call_tool(session, "browser_type", {"selector": email_selector, "text": APPLE_ID, "delay_ms": 45})
    await _click_with_fallback(session, APPLE_NEXT_BUTTON_CANDIDATES)
    await asyncio.sleep(_human_delay(1.0, 2.0))

    password_selector = await _wait_for_selector(session, APPLE_PASSWORD_SELECTORS, timeout_seconds=25.0)
    if not password_selector:
        raise OrchestratorError("Unable to find Apple password field.")

    await _call_tool(session, "browser_type", {"selector": password_selector, "text": APPLE_PASS, "delay_ms": 45})
    await _click_with_fallback(session, APPLE_NEXT_BUTTON_CANDIDATES)
    await asyncio.sleep(_human_delay(1.0, 2.0))

    verification_selector = await _wait_for_selector(session, APPLE_2FA_SELECTORS, timeout_seconds=6.0, require_visible=True)
    if verification_selector:
        code = input("[ACTION REQUIRED] Apple 2FA Code sent to device. Type code here: ").strip()
        if not code:
            raise OrchestratorError("No 2FA code entered.")

        await _call_tool(session, "browser_type", {"selector": verification_selector, "text": code, "delay_ms": 30})
        await _click_with_fallback(session, APPLE_VERIFY_BUTTON_CANDIDATES)
        await asyncio.sleep(_human_delay(1.0, 2.0))

    login_ok = await _is_logged_in(session)
    if not login_ok:
        # give one short grace period for post-auth transitions
        await asyncio.sleep(_human_delay(2.0, 3.0))
        login_ok = await _is_logged_in(session)
    if not login_ok:
        raise OrchestratorError("Apple login flow did not complete")

    await _call_tool(session, "browser_save_state", {"path": str(STATE_PATH.resolve())})
    _log(f"Saved session state to {STATE_PATH.resolve()}")


async def _collect_application_fields(session: ClientSession) -> list[dict[str, Any]]:
    script = r"""
    (() => {
      const labelByInput = new Map();
      const inputsWithFor = document.querySelectorAll('label[for]');

      inputsWithFor.forEach((label) => {
        const id = (label.getAttribute('for') || '').trim();
        if (!id) return;
        const text = (label.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
        if (text) labelByInput.set(id, text);
      });

      const selectorFor = (el) => {
        if (!el || !el.isConnected) return '';
        if (el.id) return `#${CSS.escape(el.id)}`;

        const parts = [];
        let current = el;
        let depth = 0;
        while (current && current.nodeType === 1 && current !== document.body && depth < 8) {
          let segment = current.tagName.toLowerCase();
          const parent = current.parentElement;
          if (!parent) { parts.unshift(segment); break; }
          let index = 1;
          let sibling = current.previousElementSibling;
          while (sibling) {
            if (sibling.tagName === current.tagName) index += 1;
            sibling = sibling.previousElementSibling;
          }
          segment += `:nth-of-type(${index})`;
          parts.unshift(segment);
          current = parent;
          depth += 1;
        }
        return parts.join(' > ');
      };

      const controls = Array.from(document.querySelectorAll('input, textarea, select'));
      const visible = [];

      for (const control of controls) {
        const controlType = (control.type || '').toLowerCase();
        const styles = window.getComputedStyle(control);
        if (
          !styles ||
          (controlType !== 'file' && (styles.display === 'none' || styles.visibility === 'hidden' || parseFloat(styles.opacity || '1') === 0))
        ) {
          continue;
        }

        const label = labelByInput.get((control.id || '').trim()) || '';
        const attrs = [
          control.tagName.toLowerCase(),
          controlType,
          control.name || '',
          control.id || '',
          control.getAttribute('aria-label') || '',
          control.getAttribute('placeholder') || '',
          control.getAttribute('autocomplete') || '',
          control.getAttribute('title') || '',
          label,
        ]
          .join(' ')
          .replace(/\s+/g, ' ')
          .trim()
          .toLowerCase();

        visible.push({
          selector: selectorFor(control),
          tag: control.tagName.toLowerCase(),
          type: controlType,
          name: control.name || '',
          id: control.id || '',
          placeholder: control.getAttribute('placeholder') || '',
          ariaLabel: control.getAttribute('aria-label') || '',
          label,
          text: attrs,
          disabled: !!control.disabled,
        });
      }

      return JSON.stringify(visible.filter((field) => !field.disabled));
    })();
    """

    payload = await _call_evaluate_json(session, script)
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and "error" in payload:
        return []
    if isinstance(payload, str):
        parsed = _parse_json_if_possible(payload)
        if isinstance(parsed, list):
            return parsed
    return []


async def _find_inputs_with_candidates(session: ClientSession, candidates: list[str], text: str, is_textarea: bool = False) -> str:
    last_error = None
    for selector in candidates:
        try:
            args = {"selector": selector, "text": text, "delay_ms": 40 if not is_textarea else 60}
            return await _call_tool(session, "browser_type", args)
        except OrchestratorError as exc:
            last_error = exc
            continue
    raise OrchestratorError(f"Unable to fill selector for value '{text}'. Last error: {last_error}")


async def _search_jobs(session: ClientSession, target_limit: int = TARGETS_LIMIT) -> list[dict[str, str]]:
    await _call_tool(session, "browser_navigate", {"url": "https://www.indeed.com"})
    await asyncio.sleep(_human_delay(1.2, 2.0))

    await _find_inputs_with_candidates(session, SEARCH_WHAT_CANDIDATES, SEARCH_TERM)
    await asyncio.sleep(_human_delay(0.5, 0.8))
    await _find_inputs_with_candidates(session, SEARCH_WHERE_CANDIDATES, LOCATION_TERM)
    await asyncio.sleep(_human_delay(0.5, 0.8))

    await _click_with_fallback(session, SEARCH_BUTTON_CANDIDATES)
    await asyncio.sleep(_human_delay(2.2, 3.5))

    try:
        await _call_tool(
            session,
            "browser_screenshot",
            {"path": str(DEBUG_SEARCH_SCREENSHOT)},
        )
        _log(f"Saved search page screenshot to {DEBUG_SEARCH_SCREENSHOT}")
    except OrchestratorError as exc:
        _log(f"Search screenshot failed: {exc}")

    raw = await _call_tool(session, "browser_get_content", {"selector": "body"})
    links = _extract_links_from_text(raw, limit=target_limit)

    if len(links) >= target_limit:
        return links

    fallback = await _call_evaluate_json(
        session,
        r"""
        (() => {
          const seen = new Set();
          const out = [];
          const anchors = Array.from(document.querySelectorAll('a[href]'));
          for (const a of anchors) {
            let href = a.getAttribute('href') || '';
            if (!href) continue;
            try { href = new URL(href, location.href).href; } catch (err) { continue; }
            const lower = href.toLowerCase();
            const text = (a.textContent || '').replace(/\s+/g, ' ').trim();
            if (!lower.includes('indeed.com')) continue;
            // Allow broader SERP URLs; Python canonicalization enforces strict /viewjob.
            if (!/[?&]jk=/.test(lower)) continue;
            if (/\/cmp\//.test(lower) || /\/company\//.test(lower)) continue;
            if (seen.has(lower)) continue;
            if (!text) continue;
            seen.add(lower);
            out.push({ title: text, link: href });
            if (out.length >= 10) break;
          }
          return JSON.stringify(out);
        })();
        """,
    )
    if isinstance(fallback, list):
        for item in fallback:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip()
            link = str(item.get("link", "")).strip()
            if not link or not title:
                continue
            canonical = _canonicalize_indeed_target_url(link)
            if not canonical:
                continue
            if any(canonical.lower() == existing["link"].lower() for existing in links):
                continue
            links.append({"title": title, "link": canonical})
            if len(links) >= target_limit:
                break

    if not links:
        _log("WARNING: No jobs found. Pausing for manual inspection.")
        DEBUG_PAGE_HTML.parent.mkdir(parents=True, exist_ok=True)
        try:
            page_html = await _call_tool(
                session,
                "browser_evaluate",
                {"script": "(() => { return document.documentElement.outerHTML; })();"},
            )
            DEBUG_PAGE_HTML.write_text(str(page_html), encoding="utf-8")
            _log(f"Saved page HTML to {DEBUG_PAGE_HTML}")
        except OrchestratorError as exc:
            _log(f"Failed to save debug HTML: {exc}")
            DEBUG_PAGE_HTML.write_text("", encoding="utf-8")

        if sys.stdin is not None and sys.stdin.isatty():
            input("Press Enter to continue/close...")
        else:
            _log("Non-interactive stdin detected; skipping manual pause.")

    return links[:target_limit]


async def _click_with_fallback(session: ClientSession, selectors: list[str]) -> str:
    last_error = None
    for selector in selectors:
        try:
            return await _call_tool(session, "browser_click", {"selector": selector})
        except OrchestratorError as exc:
            last_error = exc
            continue
    raise OrchestratorError(f"No matching element found. Last error: {last_error}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the orchestrator in sandbox or live mode.")
    parser.add_argument(
        "--sandbox",
        action="store_true",
        default=os.getenv("ORCHESTRATOR_SANDBOX", "1") != "0",
        help="Enable sandbox mode and skip final submission (default: enabled).",
    )
    parser.add_argument(
        "--no-sandbox",
        action="store_false",
        dest="sandbox",
        help="Disable sandbox mode and allow submission when AUTO_SUBMIT=1.",
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        default=not HEADLESS_DEFAULT,
        help="Run browser headful for observability.",
    )
    parser.add_argument("--targets-limit", type=int, default=TARGETS_LIMIT, help="Maximum jobs to process")
    parser.set_defaults(sandbox=True)
    return parser.parse_args()


async def run(sandbox: bool = True, targets_limit: int = TARGETS_LIMIT, headful: bool = not HEADLESS_DEFAULT) -> None:
    _ensure_profile_exists(PROFILE_PATH)
    _ensure_resume_artifact(PROFILE_PATH)
    profile = _normalize_profile(PROFILE_PATH)
    _log(f"Loaded profile for {profile.get('first_name', '')} {profile.get('last_name', '')}")

    if not SERVER_SCRIPT.exists():
        raise FileNotFoundError(f"Server script missing: {SERVER_SCRIPT}")

    if StdioServerParameters is not None:
        context = stdio_client(
            StdioServerParameters(
                command=PYTHON_EXECUTABLE,
                args=[str(SERVER_SCRIPT)],
            )
        )
    else:
        context = stdio_client(
            command=PYTHON_EXECUTABLE,
            args=[str(SERVER_SCRIPT)],
        )

    records: list[dict[str, Any]] = []

    async with context as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            try:
                await session.initialize()

                launch_args = {"headless": not headful}
                use_restored_state = STATE_PATH.exists()
                if use_restored_state:
                    launch_args["state_path"] = str(STATE_PATH.resolve())
                    _log("Restored Session.")
                    _log("Running in session-restored mode.")

                await _call_tool(session, "browser_launch", launch_args)
                if not use_restored_state:
                    await _perform_login(session)

                if sandbox:
                    _log("Sandbox mode active: no live submission will occur.")

                jobs = await _search_jobs(session, target_limit=targets_limit)
                if not jobs:
                    _log("No jobs found from search results")

                for job in jobs:
                    title = job.get("title", "(untitled)")
                    link = job.get("link", "")
                    record: dict[str, Any] = {
                        "title": title,
                        "link": link,
                        "status": "skipped",
                        "detail": "",
                        "filled": [],
                        "skipped_fields": [],
                    }

                    try:
                        if not link:
                            record["detail"] = "Missing job link"
                            _log(f"Skipping record missing URL: {title}")
                            records.append(record)
                            continue
                        canonical = _canonicalize_indeed_target_url(link)
                        if not canonical:
                            record["detail"] = "Skipped: target not on Indeed /viewjob allowlist"
                            _log(f"Skipped: {record['detail']} - {link}")
                            records.append(record)
                            continue
                        link = canonical
                        record["link"] = link

                        _log(f"Opening job: {title}")
                        await _call_tool(session, "browser_navigate", {"url": link})
                        await asyncio.sleep(_human_delay(1.2, 2.3))

                        apply_selector = await _click_apply_control(session)
                        if not apply_selector:
                            record["detail"] = "No Apply/Easy Apply control found"
                            _log(f"Skipped: {record['detail']} - {link}")
                            records.append(record)
                            continue
                        await asyncio.sleep(_human_delay(2.0, 3.0))

                        signature = await _call_evaluate_json(
                            session,
                            "(() => { return JSON.stringify({ url: window.location.href, text: (document.body ? document.body.innerText : '').slice(0, 5000).toLowerCase() }); })();",
                        )
                        page_url = signature.get("url", "") if isinstance(signature, dict) else ""
                        page_text = signature.get("text", "") if isinstance(signature, dict) else ""

                        if _is_redirect_or_ats(page_url, page_text):
                            record["detail"] = "Skipped due to external ATS redirect (Workday/Taleo/etc.)"
                            _log(f"Skipped: {record['detail']}")
                            records.append(record)
                            continue

                        discovered = await _collect_application_fields(session)
                        mapped = _map_fields_to_profile(profile, discovered)
                        if not mapped:
                            record["detail"] = "No recognized form fields found"
                            _log(f"Skipped: {record['detail']}")
                            records.append(record)
                            continue

                        filled, skipped = await _type_into_fields(session, mapped)
                        record["filled"] = filled
                        record["skipped_fields"] = skipped

                        submit_selector = await _find_submit_selector(session)
                        if AUTO_SUBMIT and (not sandbox) and submit_selector:
                            await _call_tool(session, "browser_click", {"selector": submit_selector})
                            record["status"] = "submitted"
                            record["detail"] = "Submitted via detected submit control"
                        elif sandbox:
                            record["status"] = "ready_to_submit"
                            if not record["detail"]:
                                record["detail"] = "Ready to Submit (sandbox mode)"
                        else:
                            record["status"] = "ready_to_submit"
                            if not record["detail"]:
                                record["detail"] = "Ready to Submit"

                    except OrchestratorError as exc:
                        record["status"] = "skipped"
                        record["detail"] = str(exc)
                        _log(f"Job failed: {record['detail']}")

                    records.append(record)

            except Exception as exc:
                _log(f"Fatal protocol failure: {exc}")
            finally:
                try:
                    await _call_tool(session, "browser_close")
                except Exception as exc:  # pragma: no cover - best-effort close
                    _log(f"Close warning: {exc}")

    output = {
        "query": {
            "what": SEARCH_TERM,
            "where": LOCATION_TERM,
            "count": targets_limit,
        },
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "results": records,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(output, indent=2), encoding="utf-8")
    _log(f"Saved targets report to {REPORT_PATH}")


if __name__ == "__main__":
    cli = _parse_args()
    asyncio.run(
        run(
            sandbox=bool(cli.sandbox),
            targets_limit=max(1, int(cli.targets_limit)),
            headful=bool(cli.headful),
        )
    )
