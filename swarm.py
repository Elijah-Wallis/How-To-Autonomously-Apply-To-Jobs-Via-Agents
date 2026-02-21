#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent
PROFILE_PATH = ROOT / "profile.json"
TARGETS_PATH = ROOT / "targets.json"
STATE_PATH = ROOT / ".state" / "runtime_state.json"
LOG_DIR = ROOT / "logs"
PROOF_DIR = ROOT / "proof"

TTL_SECONDS = 90
MAX_BATCH = 3
MAX_SELF_HEAL_ATTEMPTS = 15

SUCCESS_TEXT_MARKERS = [
    "thank you",
    "application submitted",
    "confirmation",
    "application received",
]
SUCCESS_URL_MARKERS = [
    "thank-you",
    "application-submitted",
    "confirmation",
    "success",
    "complete",
]

TARGETS = [
    {"company": "Curtin Maritime", "url": "https://curtinmaritime.bamboohr.com/jobs"},
    {"company": "Great Lakes Dredge & Dock", "url": "https://gldd.com/careers/"},
    {"company": "Weeks Marine", "url": "https://kiewitcareers.kiewit.com/Weeks"},
    {"company": "Manson Construction", "url": "https://www.mansonconstruction.com/careers"},
    {"company": "Callan Marine", "url": "https://www.callanmarineltd.com/careers"},
    {"company": "Cashman Dredging", "url": "https://www.jaycashman.com/careers/"},
    {"company": "Viking Dredging", "url": "https://www.vikingdredging.com/join-our-team.php"},
    {"company": "Muddy Water Dredging", "url": "https://mwdredging.com/job-opportunities/"},
    {"company": "Orion Government Services", "url": "https://oriongov.com"},
    {"company": "Moran Towing", "url": "https://www.morantug.com/careers-at-moran/"},
]

BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}
BLOCKED_URL_SNIPPETS = {
    "google-analytics",
    "googletagmanager",
    "doubleclick",
    "facebook.net",
    "hotjar",
    "segment",
    ".mp4",
    ".webm",
    ".mov",
    ".avi",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".css",
}

COOKIE_HINTS = ["accept", "accept all", "allow all", "i agree", "agree"]
NAV_HINTS = ["careers", "jobs", "join", "opportunities", "open positions", "deckhand", "entry"]
APPLY_HINTS = ["apply", "apply now", "easy apply", "start application", "continue application", "join now"]
SUBMIT_HINTS = ["submit", "submit application", "finish application", "complete application", "review and submit", "send"]

INJECT_HELPER_JS = r"""
(() => {
  if (window.__MARITIME_SWARM__) return;
  const norm = (v) => String(v || '').toLowerCase().replace(/\s+/g, ' ').trim();
  const fields = () => Array.from(document.querySelectorAll('input, textarea, select'));
  const desc = (el) => norm([
    el.getAttribute('name'),
    el.getAttribute('id'),
    el.getAttribute('placeholder'),
    el.getAttribute('aria-label'),
    el.closest('label') ? el.closest('label').innerText : '',
    el.closest('fieldset') ? el.closest('fieldset').innerText : ''
  ].join(' '));

  function setValue(el, value) {
    if (!el || value === undefined || value === null || value === '') return false;
    if (el.disabled || el.readOnly) return false;
    const tag = (el.tagName || '').toLowerCase();
    const type = norm(el.getAttribute('type'));
    if (tag === 'select') {
      const want = norm(value);
      const options = Array.from(el.options || []);
      const hit = options.find(o => norm(o.textContent).includes(want) || norm(o.value).includes(want));
      if (!hit) return false;
      el.value = hit.value;
      el.dispatchEvent(new Event('change', { bubbles: true }));
      return true;
    }
    if (type === 'radio' || type === 'checkbox') return false;
    el.focus();
    el.value = String(value);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
  }

  function clickChoices(questionHints, optionHints) {
    const nodes = Array.from(document.querySelectorAll("input[type='radio'],input[type='checkbox']"));
    for (const n of nodes) {
      const q = desc(n);
      if (!questionHints.some(h => q.includes(norm(h)))) continue;
      const label = n.closest('label') || (n.id ? document.querySelector(`label[for='${n.id}']`) : null);
      const text = norm((label ? label.innerText : '') + ' ' + (n.value || ''));
      if (optionHints.some(h => text.includes(norm(h)))) {
        n.click();
        n.dispatchEvent(new Event('change', { bubbles: true }));
        return true;
      }
    }
    return false;
  }

  function fillProfile(p) {
    const map = {
      first_name: ['first name', 'firstname', 'given name'],
      last_name: ['last name', 'lastname', 'surname', 'family name'],
      full_name: ['full name', 'your name', 'name'],
      email: ['email', 'e-mail'],
      phone: ['phone', 'mobile', 'telephone', 'contact number'],
      address_line1: ['address', 'street'],
      city: ['city'],
      state: ['state', 'province'],
      zip: ['zip', 'postal'],
      pitch: ['cover letter', 'summary', 'message', 'why', 'about you', 'introduction'],
      sea_days_note: ['sea days', 'offshore', 'additional information', 'notes']
    };

    let filled = 0;
    const all = fields();
    for (const [k, v] of Object.entries(p || {})) {
      const aliases = map[k] || [k];
      for (const el of all) {
        const d = desc(el);
        if (!aliases.some(a => d.includes(norm(a)))) continue;
        if (setValue(el, v)) filled += 1;
      }
    }
    return filled;
  }

  function applyEeo(e) {
    let changed = 0;
    const selects = Array.from(document.querySelectorAll('select'));
    for (const s of selects) {
      const d = desc(s);
      if (d.includes('race') || d.includes('ethnicity')) {
        if (setValue(s, e.race || 'Black or African American')) changed += 1;
      }
      if (d.includes('veteran') || d.includes('protected veteran')) {
        if (setValue(s, e.veteran || 'No')) changed += 1;
      }
      if (d.includes('disability')) {
        if (setValue(s, e.disability || 'No')) changed += 1;
      }
    }
    if (clickChoices(['race', 'ethnicity'], [e.race || 'Black or African American'])) changed += 1;
    if (clickChoices(['veteran', 'protected veteran'], ['No', 'Decline to Answer', e.veteran || 'No'])) changed += 1;
    if (clickChoices(['disability'], ['No', 'I do not wish to disclose', e.disability || 'No'])) changed += 1;
    return changed;
  }

  function clickByHints(hints) {
    const hs = (hints || []).map(norm).filter(Boolean);
    const controls = Array.from(document.querySelectorAll("button,a,input[type='submit'],input[type='button']"));
    for (const c of controls) {
      const text = norm(c.innerText || c.value || c.getAttribute('aria-label') || c.getAttribute('title') || '');
      if (!text) continue;
      if (hs.some(h => text.includes(h))) {
        c.focus();
        c.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
        c.dispatchEvent(new Event('click', { bubbles: true }));
        return text;
      }
    }
    return '';
  }

  function detectBlockers() {
    const bodyText = norm(document.body ? document.body.innerText : '');
    const captcha = /captcha|recaptcha|hcaptcha|cloudflare.*challenge|verify.*human/i.test(bodyText) ||
                     document.querySelector('iframe[src*="recaptcha"],iframe[src*="hcaptcha"],iframe[src*="challenges.cloudflare"]');
    const sms = /enter.*code|verify.*phone|text.*code|sms.*verification/i.test(bodyText);
    const ats = /taleo|workday|greenhouse|lever|bamboohr|icims|jobvite|smartrecruiters/i.test(window.location.href);
    return { captcha: !!captcha, sms: !!sms, ats: !!ats, blocked: !!(captcha || sms) };
  }

  window.__MARITIME_SWARM__ = { fillProfile, applyEeo, clickByHints, detectBlockers };
})();
"""


def utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "target"


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_profile() -> dict[str, Any]:
    profile = read_json(PROFILE_PATH, {})
    profile.setdefault("first_name", "Elijah")
    profile.setdefault("last_name", "Wallis")
    profile.setdefault("email", "elijahcwallis@gmail.com")
    profile.setdefault("phone", "985-991-4360")
    profile.setdefault("address_line1", "3201 Wynwood Dr")
    profile.setdefault("city", "Plano")
    profile.setdefault("state", "TX")
    profile.setdefault("zip", "75074")
    profile.setdefault("resume_path", "./resume.pdf")
    profile.setdefault("sea_days_note", "250 documented sea days with company letters attached.")
    profile.setdefault("eeo_defaults", {"race": "Black or African American", "veteran": "No", "disability": "No"})
    return profile


def load_state() -> dict[str, Any]:
    return read_json(
        STATE_PATH,
        {
            "heal_count": 0,
            "extra_apply_hints": [],
            "extra_submit_hints": [],
            "extra_success_markers": [],
        },
    )


def save_state(state: dict[str, Any]) -> None:
    write_json(STATE_PATH, state)


def self_heal(attempt: int) -> dict[str, Any]:
    state = load_state()
    state["heal_count"] = int(state.get("heal_count", 0)) + 1

    log_path = LOG_DIR / f"swarm_attempt_{attempt}.log"
    text = log_path.read_text(encoding="utf-8", errors="ignore").lower() if log_path.exists() else ""

    apply_pool = ["continue", "next", "proceed", "begin application", "start", "quick apply"]
    submit_pool = ["confirm", "complete", "final submit", "send", "review"]
    success_pool = ["thanks for applying", "application has been submitted", "we received your application"]

    for h in apply_pool:
        if h not in state["extra_apply_hints"] and ("timeout" in text or "no_success_signal" in text or "incomplete" in text):
            state["extra_apply_hints"].append(h)
            break
    for h in submit_pool:
        if h not in state["extra_submit_hints"] and ("submit" in text or "incomplete" in text or "no_success_signal" in text):
            state["extra_submit_hints"].append(h)
            break
    for h in success_pool:
        if h not in state["extra_success_markers"]:
            state["extra_success_markers"].append(h)
            break

    save_state(state)
    return state


async def click_by_hints(page: Any, hints: list[str]) -> str:
    """Pure DOM-only clicking via JS eval - no mouse, no typing delays."""
    try:
        hit = await page.evaluate(
            "(h) => (window.__MARITIME_SWARM__ ? window.__MARITIME_SWARM__.clickByHints(h) : '')",
            hints,
        )
        if isinstance(hit, str) and hit.strip():
            return hit.strip()
    except Exception:
        pass
    
    # Fallback: pure JS eval only, no locator.click()
    try:
        for hint in hints:
            hit = await page.evaluate(f"""
                (() => {{
                    const norm = (v) => String(v || '').toLowerCase().replace(/\\s+/g, ' ').trim();
                    const h = norm('{hint}');
                    const controls = Array.from(document.querySelectorAll("button,a,input[type='submit'],input[type='button']"));
                    for (const c of controls) {{
                        const text = norm(c.innerText || c.value || c.getAttribute('aria-label') || c.getAttribute('title') || '');
                        if (text.includes(h)) {{
                            c.focus();
                            c.dispatchEvent(new MouseEvent('click', {{ bubbles: true, cancelable: true }}));
                            c.dispatchEvent(new Event('click', {{ bubbles: true }}));
                            return text;
                        }}
                    }}
                    return '';
                }})()
            """)
            if isinstance(hit, str) and hit.strip():
                return hit.strip()
    except Exception:
        pass
    return ""


async def apply_profile(page: Any, profile: dict[str, Any]) -> tuple[int, int]:
    payload = {
        "first_name": profile.get("first_name", ""),
        "last_name": profile.get("last_name", ""),
        "full_name": f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip(),
        "email": profile.get("email", ""),
        "phone": profile.get("phone", ""),
        "address_line1": profile.get("address_line1", ""),
        "city": profile.get("city", ""),
        "state": profile.get("state", ""),
        "zip": profile.get("zip", ""),
        "pitch": profile.get("pitch", ""),
        "sea_days_note": profile.get("sea_days_note", ""),
    }
    eeo = profile.get("eeo_defaults", {})
    out = await page.evaluate(
        """(data) => {
            if (!window.__MARITIME_SWARM__) return {filled: 0, eeo: 0};
            const filled = window.__MARITIME_SWARM__.fillProfile(data.payload || {});
            const eeo = window.__MARITIME_SWARM__.applyEeo(data.eeo || {});
            return {filled, eeo};
        }""",
        {"payload": payload, "eeo": eeo},
    )
    return int(out.get("filled", 0)), int(out.get("eeo", 0))


async def upload_resume(page: Any, path: Path) -> int:
    if not path.exists():
        return 0
    inputs = page.locator("input[type='file']")
    c = await inputs.count()
    uploaded = 0
    for i in range(c):
        try:
            await inputs.nth(i).set_input_files(str(path.resolve()))
            uploaded += 1
        except Exception:
            continue
    return uploaded


async def check_success(page: Any, extra_markers: list[str], screenshot_path: Path) -> dict[str, Any]:
    """Extract text via pure JS eval, check for confirmation markers."""
    text = ""
    try:
        text = await page.evaluate("""() => {
            const body = document.body || document.documentElement;
            return (body.innerText || body.textContent || '').toLowerCase();
        }""")
    except Exception:
        text = ""
    
    url = page.url.lower()
    markers = SUCCESS_TEXT_MARKERS + list(extra_markers or [])
    text_hits = [m for m in markers if m.lower() in text]
    url_ok = any(k in url for k in SUCCESS_URL_MARKERS)
    screenshot_ok = screenshot_path.exists()
    
    # ACCEPTANCE GATE: Only ok if we have confirmation proof (text or URL), not just screenshot
    ok = bool(text_hits or url_ok)
    
    return {
        "ok": ok,
        "text_hits": text_hits,
        "url_match": url_ok,
        "screenshot_ok": screenshot_ok,
        "final_url": page.url,
    }


async def worker(browser: Any, sem: asyncio.Semaphore, target: dict[str, str], profile: dict[str, Any], state: dict[str, Any], attempt: int) -> dict[str, Any]:
    company = target["company"]
    url = target["url"]
    slug = slugify(company)

    async with sem:
        context = await browser.new_context(ignore_https_errors=True)
        page = await context.new_page()

        async def route_handler(route: Any) -> None:
            """NETWORK BLOCKING: Abort ALL image/video/font/CSS requests before render."""
            req = route.request
            u = req.url.lower()
            # Block by resource type
            if req.resource_type in BLOCKED_RESOURCE_TYPES:
                await route.abort()
                return
            # Block by URL extension
            if any(ext in u for ext in [".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".webm", ".css", ".woff2", ".ttf", ".woff", ".otf"]):
                await route.abort()
                return
            # Block by URL snippet
            if any(s in u for s in BLOCKED_URL_SNIPPETS):
                await route.abort()
                return
            await route.continue_()

        await page.route("**/*", route_handler)
        await page.add_init_script(INJECT_HELPER_JS)

        status = "INCOMPLETE"
        detail = ""
        proof: dict[str, Any] = {}

        async def flow() -> None:
            nonlocal status, detail, proof
            # HEADLESS + DOM-ONLY: Pure Playwright JS eval only
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.evaluate("() => new Promise(r => setTimeout(r, 500))")  # Minimal delay via JS

            # FAIL-FAST: Detect Captcha/SMS/ATS blockers
            blockers = await page.evaluate("() => (window.__MARITIME_SWARM__ ? window.__MARITIME_SWARM__.detectBlockers() : {blocked: false})")
            if blockers.get("blocked"):
                status = "BLOCKED"
                detail = f"Blocked - External: captcha={blockers.get('captcha')}, sms={blockers.get('sms')}"
                success_png = ROOT / "proof" / f"{slug}_attempt{attempt}_blocked.png"
                try:
                    await page.screenshot(path=str(success_png), full_page=True)
                except Exception:
                    pass
                proof = {
                    "screenshot": f"proof/{success_png.name}" if success_png.exists() else "",
                    "final_url": page.url,
                    "text_hits": [],
                    "url_match": False,
                    "screenshot_ok": success_png.exists(),
                }
                return

            await click_by_hints(page, COOKIE_HINTS)
            await click_by_hints(page, NAV_HINTS)

            apply_hints = APPLY_HINTS + state.get("extra_apply_hints", [])
            submit_hints = SUBMIT_HINTS + state.get("extra_submit_hints", [])

            filled = 0
            eeo = 0
            uploaded = 0
            # BATCHING: Iterate through form filling cycles
            for cycle in range(3):
                f, e = await apply_profile(page, profile)
                filled = max(filled, f)
                eeo = max(eeo, e)
                uploaded = max(uploaded, await upload_resume(page, ROOT / str(profile.get("resume_path", "./resume.pdf"))))
                await click_by_hints(page, apply_hints)
                await page.evaluate("() => new Promise(r => setTimeout(r, 300))")  # Minimal delay via JS
                await click_by_hints(page, submit_hints)
                await page.evaluate("() => new Promise(r => setTimeout(r, 400))")  # Minimal delay via JS
                
                # Check for success after each cycle
                success_png = ROOT / "proof" / f"{slug}_attempt{attempt}_success.png"
                try:
                    await page.screenshot(path=str(success_png), full_page=True)
                except Exception:
                    pass
                success = await check_success(page, state.get("extra_success_markers", []), success_png)
                if success["ok"]:
                    proof = {
                        "screenshot": f"proof/{success_png.name}",
                        "final_url": success.get("final_url", page.url),
                        "text_hits": success.get("text_hits", []),
                        "url_match": bool(success.get("url_match", False)),
                        "screenshot_ok": bool(success.get("screenshot_ok", False)),
                        "filled_count": filled,
                        "eeo_actions": eeo,
                        "resume_uploads": uploaded,
                    }
                    status = "COMPLETE"
                    detail = "confirmation_or_success_proof_verified"
                    return

            # Final check if no success found in cycles
            success_png = ROOT / "proof" / f"{slug}_attempt{attempt}_success.png"
            try:
                await page.screenshot(path=str(success_png), full_page=True)
            except Exception:
                pass
            success = await check_success(page, state.get("extra_success_markers", []), success_png)

            proof = {
                "screenshot": f"proof/{success_png.name}",
                "final_url": success.get("final_url", page.url),
                "text_hits": success.get("text_hits", []),
                "url_match": bool(success.get("url_match", False)),
                "screenshot_ok": bool(success.get("screenshot_ok", False)),
                "filled_count": filled,
                "eeo_actions": eeo,
                "resume_uploads": uploaded,
            }

            # ACCEPTANCE GATE: COMPLETE ONLY when confirmation proof exists
            if success["ok"]:
                status = "COMPLETE"
                detail = "confirmation_or_success_proof_verified"
            else:
                status = "INCOMPLETE"
                detail = "no_success_signal"

        try:
            await asyncio.wait_for(flow(), timeout=TTL_SECONDS)
        except asyncio.TimeoutError:
            success_png = ROOT / "proof" / f"{slug}_attempt{attempt}_success.png"
            try:
                await page.screenshot(path=str(success_png), full_page=True)
            except Exception:
                pass
            # Check if we have confirmation proof despite timeout
            success = await check_success(page, state.get("extra_success_markers", []), success_png)
            if success["ok"]:
                status = "COMPLETE"
                detail = f"timeout_{TTL_SECONDS}s_with_confirmation_proof"
                proof = {
                    "screenshot": f"proof/{success_png.name}",
                    "final_url": success.get("final_url", page.url),
                    "text_hits": success.get("text_hits", []),
                    "url_match": bool(success.get("url_match", False)),
                    "screenshot_ok": success_png.exists(),
                }
            else:
                status = "INCOMPLETE"
                detail = f"timeout_{TTL_SECONDS}s_no_confirmation_proof"
                proof = {
                    "screenshot": f"proof/{success_png.name}",
                    "final_url": page.url,
                    "text_hits": [],
                    "url_match": False,
                    "screenshot_ok": success_png.exists(),
                }
        except PlaywrightTimeoutError:
            success_png = ROOT / "proof" / f"{slug}_attempt{attempt}_success.png"
            try:
                await page.screenshot(path=str(success_png), full_page=True)
            except Exception:
                pass
            # Check if we have confirmation proof despite timeout
            success = await check_success(page, state.get("extra_success_markers", []), success_png)
            if success["ok"]:
                status = "COMPLETE"
                detail = "playwright_timeout_with_confirmation_proof"
                proof = {
                    "screenshot": f"proof/{success_png.name}",
                    "final_url": success.get("final_url", page.url),
                    "text_hits": success.get("text_hits", []),
                    "url_match": bool(success.get("url_match", False)),
                    "screenshot_ok": success_png.exists(),
                }
            else:
                status = "INCOMPLETE"
                detail = "playwright_timeout_no_confirmation_proof"
                proof = {"screenshot": f"proof/{success_png.name}", "final_url": page.url, "text_hits": [], "url_match": False, "screenshot_ok": success_png.exists()}
        except Exception as exc:
            success_png = ROOT / "proof" / f"{slug}_attempt{attempt}_success.png"
            try:
                await page.screenshot(path=str(success_png), full_page=True)
            except Exception:
                pass
            # Check if we have confirmation proof despite exception
            success = await check_success(page, state.get("extra_success_markers", []), success_png)
            if success["ok"]:
                status = "COMPLETE"
                detail = f"exception:{exc.__class__.__name__}:{exc}:with_confirmation_proof"
                proof = {
                    "screenshot": f"proof/{success_png.name}",
                    "final_url": success.get("final_url", page.url),
                    "text_hits": success.get("text_hits", []),
                    "url_match": bool(success.get("url_match", False)),
                    "screenshot_ok": success_png.exists(),
                }
            else:
                status = "INCOMPLETE"
                detail = f"exception:{exc.__class__.__name__}:{exc}:no_confirmation_proof"
                proof = {"screenshot": f"proof/{success_png.name}", "final_url": page.url, "text_hits": [], "url_match": False, "screenshot_ok": success_png.exists()}
        finally:
            await context.close()

        return {
            "company": company,
            "url": url,
            "status": status,
            "detail": detail,
            "last_attempt": attempt,
            "proof": proof,
            "updated_at": utc_now(),
        }


async def run_swarm(attempt: int, batch_size: int, headful: bool) -> dict[str, Any]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PROOF_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

    profile = load_profile()
    state = load_state()
    sem = asyncio.Semaphore(max(1, min(batch_size, MAX_BATCH)))

    async with async_playwright() as p:
        # HEADLESS + DOM-ONLY: Always headless, pure JS eval
        # Add sandbox-friendly launch args to avoid crashes
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-software-rasterizer",
            ]
        )
        # BATCHING: Process in batches of 3, complete batch before next
        results = []
        for i in range(0, len(TARGETS), batch_size):
            batch = TARGETS[i:i + batch_size]
            batch_tasks = [asyncio.create_task(worker(browser, sem, t, profile, state, attempt)) for t in batch]
            batch_results = await asyncio.gather(*batch_tasks)
            results.extend(batch_results)
        await browser.close()

    complete = sum(1 for r in results if r.get("status") == "COMPLETE")
    payload = {
        "generated_at": utc_now(),
        "attempt": attempt,
        "batch_size": max(1, min(batch_size, MAX_BATCH)),
        "ttl_seconds": TTL_SECONDS,
        "max_self_heal_attempts": MAX_SELF_HEAL_ATTEMPTS,
        "results": results,
        "summary": {
            "total": len(results),
            "complete": complete,
            "incomplete": len(results) - complete,
        },
    }
    write_json(TARGETS_PATH, payload)
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Maritime L5 swarm runner")
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=3)
    parser.add_argument("--headful", action="store_true")
    parser.add_argument("--self-heal", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.self_heal:
        state = self_heal(max(1, int(args.attempt)))
        print(json.dumps({"self_heal": True, "state": state}, indent=2))
        return

    payload = asyncio.run(run_swarm(max(1, int(args.attempt)), max(1, min(int(args.batch_size), MAX_BATCH)), bool(args.headful)))
    print(json.dumps(payload["summary"], indent=2))


if __name__ == "__main__":
    main()
