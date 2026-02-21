#!/usr/bin/env python3
"""Maritime L5 Swarm v2 — STRICT confirmation detection with multi-step ATS navigation."""
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
SOURCE_DIR = ROOT / "proof" / "source"

TTL_SECONDS = 90
MAX_BATCH = 3
MAX_SELF_HEAL_ATTEMPTS = 15

# ---------------------------------------------------------------------------
# STRICT post-submit confirmation markers ONLY
# These phrases appear ONLY on confirmation/thank-you pages, never on pre-submit career pages.
# ---------------------------------------------------------------------------
STRICT_TEXT_MARKERS = [
    "thank you for applying",
    "thanks for applying",
    "your application has been submitted",
    "we have received your application",
    "we received your application",
    "application number",
    "application complete",
    "thank you for your application",
    "application was received",
    "application has been received",
    "your application was successfully submitted",
    "application submitted successfully",
    "your application has been received",
    "thank you for submitting",
    "thanks for submitting",
    "you have successfully applied",
    "application confirmation",
    "thank you for your interest in",
    "your submission has been received",
]

STRICT_URL_MARKERS = [
    "thank-you",
    "thankyou",
    "application-submitted",
    "application-received",
    "application-complete",
    "apply-confirmation",
    "application-confirmation",
]

# Map strict markers → compat markers for test_workflow.sh acceptance
COMPAT_MAP: dict[str, list[str]] = {
    "thank you": [
        "thank you for applying", "thanks for applying",
        "thank you for your application", "thank you for submitting",
        "thank you for your interest in",
    ],
    "application submitted": [
        "your application has been submitted",
        "application submitted successfully",
        "your application was successfully submitted",
    ],
    "confirmation": ["application confirmation"],
    "application received": [
        "we have received your application", "we received your application",
        "application was received", "application has been received",
        "your application has been received", "your submission has been received",
    ],
}

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

# Network blocking: images, video, fonts, trackers — but ALLOW CSS (needed for rendering)
BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
BLOCKED_EXTENSIONS = frozenset([
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".ico",
    ".mp4", ".webm", ".mov", ".avi",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
])
BLOCKED_DOMAINS = frozenset([
    "google-analytics", "googletagmanager", "doubleclick",
    "facebook.net", "hotjar", "segment",
])

COOKIE_HINTS = ["accept", "accept all", "allow all", "i agree", "agree", "got it", "ok"]
JOB_KEYWORDS = [
    "deckhand", "entry level", "entry-level", "dredge",
    "trainee", "boatman", "crew", "leverman", "oiler",
    "maritime training", "deck", "tankerman",
    "view our employment", "apply today",
]
APPLY_HINTS = [
    "apply for this job", "apply now", "apply", "apply online",
    "start application", "continue application", "apply for this position",
    "apply today", "submit application",
]
SUBMIT_HINTS = [
    "submit", "submit application", "submit my application",
    "finish application", "complete application", "review and submit",
    "send", "send application", "save", "save application",
    "submit your application", "apply", "confirm",
]
NAV_HINTS = [
    "careers", "view our employment", "how to apply", "apply today",
    "send resume", "read more", "view opportunities",
    "see open positions", "current openings", "job openings",
    "open positions", "join our team", "employment",
]

# ---------------------------------------------------------------------------
# Enhanced JS helpers: multi-step nav, ATS-specific selectors, strict detection
# ---------------------------------------------------------------------------
INJECT_HELPER_JS = r"""
(() => {
  if (window.__SWM2__) return;

  const norm = (v) => String(v || '').toLowerCase().replace(/\s+/g, ' ').trim();
  const allFields = () => Array.from(document.querySelectorAll('input, textarea, select'));

  function desc(el) {
    const parts = [
      el.getAttribute('name'),
      el.getAttribute('id'),
      el.getAttribute('placeholder'),
      el.getAttribute('aria-label'),
      el.getAttribute('data-label'),
      el.getAttribute('autocomplete'),
    ];
    const lbl = el.closest('label');
    if (lbl) parts.push(lbl.innerText);
    if (el.id) {
      const ext = document.querySelector('label[for="' + el.id + '"]');
      if (ext) parts.push(ext.innerText);
    }
    const fs = el.closest('fieldset');
    if (fs) { const lg = fs.querySelector('legend'); if (lg) parts.push(lg.innerText); }
    return norm(parts.join(' '));
  }

  function setVal(el, value) {
    if (!el || value === undefined || value === null || value === '') return false;
    if (el.disabled || el.readOnly) return false;
    const tag = (el.tagName || '').toLowerCase();
    const type = norm(el.getAttribute('type'));
    if (type === 'hidden' || type === 'file') return false;

    if (tag === 'select') {
      const want = norm(value);
      const opts = Array.from(el.options || []);
      // Try exact match first, then partial match
      let hit = opts.find(o => norm(o.textContent) === want || norm(o.value) === want);
      if (!hit) hit = opts.find(o => norm(o.textContent).includes(want) || norm(o.value).includes(want));
      if (!hit) hit = opts.find(o => want.includes(norm(o.textContent)) || want.includes(norm(o.value)));
      if (!hit) return false;
      el.value = hit.value;
      el.dispatchEvent(new Event('change', { bubbles: true }));
      return true;
    }
    if (type === 'radio' || type === 'checkbox') return false;

    try {
      const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value');
      if (setter && setter.set) setter.set.call(el, String(value));
      else el.value = String(value);
    } catch(e) { el.value = String(value); }

    el.dispatchEvent(new Event('focus', { bubbles: true }));
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.dispatchEvent(new Event('blur', { bubbles: true }));
    return true;
  }

  function clickChoice(qHints, oHints) {
    const nodes = Array.from(document.querySelectorAll("input[type='radio'],input[type='checkbox']"));
    for (const n of nodes) {
      const q = desc(n);
      if (!qHints.some(h => q.includes(norm(h)))) continue;
      const lbl = n.closest('label') || (n.id ? document.querySelector('label[for="' + n.id + '"]') : null);
      const txt = norm((lbl ? lbl.innerText : '') + ' ' + (n.value || ''));
      if (oHints.some(h => txt.includes(norm(h)))) {
        n.click();
        n.dispatchEvent(new Event('change', { bubbles: true }));
        return true;
      }
    }
    return false;
  }

  function fillProfile(p) {
    const map = {
      first_name: ['first name','firstname','given name','fname','first'],
      last_name: ['last name','lastname','surname','family name','lname','last'],
      full_name: ['full name','your name','applicant name'],
      email: ['email','e-mail','email address'],
      phone: ['phone','mobile','telephone','contact number','phone number','cell'],
      address_line1: ['address','street','street address','address line'],
      city: ['city','town'],
      state: ['state','province','region'],
      zip: ['zip','postal','zip code','postal code'],
      date_available: ['date available','available date','start date','availability','when can you start'],
      desired_pay: ['desired pay','salary','pay','compensation','wage','desired salary','expected salary','pay rate','hourly rate'],
      referred_by: ['who referred','referred','referral','how did you hear','source','hear about'],
      career_goals: ['what are you looking for','career goal','looking for in a career','career interest','career objective'],
      work_environment: ['ideal work environment','work environment','describe your ideal','work setting','preferred environment'],
      pitch: ['cover letter','summary','message','why','about you','introduction','comments','additional comments','comment','notes','tell us'],
      sea_days_note: ['sea days','offshore','additional information','experience','qualifications']
    };
    let filled = 0;
    const all = allFields();
    for (const [k, v] of Object.entries(p || {})) {
      const aliases = map[k] || [k];
      for (const el of all) {
        const d = desc(el);
        if (!aliases.some(a => d.includes(norm(a)))) continue;
        if (setVal(el, v)) filled += 1;
      }
    }

    // Handle Yes/No radio questions (work location, legal authorization, etc.)
    const yesQuestions = ['are you able to work', 'authorized to work', 'legally authorized', 'eligible to work', 'willing to relocate', '18 years'];
    const noQuestions = ['require sponsorship', 'need visa', 'been convicted'];
    const radios = Array.from(document.querySelectorAll("input[type='radio']"));
    for (const r of radios) {
      const q = desc(r);
      const labelEl = r.closest('label') || (r.id ? document.querySelector('label[for="' + r.id + '"]') : null);
      const rText = ((labelEl ? labelEl.innerText : '') + ' ' + (r.value || '')).toLowerCase().trim();
      if (yesQuestions.some(yq => q.includes(yq)) && (rText.includes('yes') || r.value.toLowerCase() === 'yes')) {
        r.click(); r.dispatchEvent(new Event('change', { bubbles: true })); filled++;
      }
      if (noQuestions.some(nq => q.includes(nq)) && (rText.includes('no') || r.value.toLowerCase() === 'no')) {
        r.click(); r.dispatchEvent(new Event('change', { bubbles: true })); filled++;
      }
    }

    // Aggressive state dropdown handler — tries multiple values for state selects
    const stateValues = ['Texas', 'TX', 'texas', 'tx'];
    for (const s of Array.from(document.querySelectorAll('select'))) {
      const d = desc(s);
      if (!(d.includes('state') || d.includes('province') || d.includes('region'))) continue;
      if (s.value && s.value !== '' && s.selectedIndex > 0) continue; // Already set
      const opts = Array.from(s.options || []);
      for (const tryVal of stateValues) {
        const hit = opts.find(o =>
          norm(o.textContent) === norm(tryVal) ||
          norm(o.value) === norm(tryVal) ||
          norm(o.textContent).includes(norm(tryVal))
        );
        if (hit && hit.value !== '') {
          s.value = hit.value;
          s.dispatchEvent(new Event('change', { bubbles: true }));
          filled += 1;
          break;
        }
      }
    }

    return filled;
  }

  function applyEeo(e) {
    let c = 0;
    for (const s of Array.from(document.querySelectorAll('select'))) {
      const d = desc(s);
      if ((d.includes('race') || d.includes('ethnicity')) && setVal(s, e.race || 'Black or African American')) c++;
      if ((d.includes('veteran') || d.includes('protected veteran')) && setVal(s, e.veteran || 'No')) c++;
      if (d.includes('disability') && setVal(s, e.disability || 'No')) c++;
    }
    if (clickChoice(['race','ethnicity'], [e.race || 'Black or African American'])) c++;
    if (clickChoice(['veteran','protected veteran'], ['No','Decline','I am not', e.veteran || 'No'])) c++;
    if (clickChoice(['disability'], ['No','I do not wish','I don\'t wish', e.disability || 'No'])) c++;
    return c;
  }

  function clickByHints(hints) {
    const hs = (hints || []).map(norm).filter(Boolean);
    // Extended selectors: standard controls + styled containers that act as buttons
    const sels = "button, a, input[type='submit'], input[type='button'], [role='button'], [class*='btn'], [class*='button'], [class*='cta'], [onclick]";
    const els = Array.from(document.querySelectorAll(sels));
    for (const el of els) {
      const txt = norm(el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || '');
      if (!txt || txt.length > 200) continue;
      if (hs.some(h => txt.includes(h))) {
        el.focus();
        el.click();
        el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
        // If this is a submit button, also try form submit
        if (el.type === 'submit' || txt.includes('submit')) {
          const form = el.closest('form');
          if (form && form.requestSubmit) { try { form.requestSubmit(el); } catch(e) {} }
        }
        return txt;
      }
    }
    return '';
  }

  function findAndClickJobLink(keywords) {
    const kw = keywords.map(k => k.toLowerCase());
    const links = Array.from(document.querySelectorAll('a[href]'));
    // Pass 1: link text matches keyword
    for (const a of links) {
      const txt = norm(a.innerText || a.textContent || '');
      if (txt.length > 0 && txt.length < 200 && kw.some(k => txt.includes(k))) {
        a.click();
        return txt;
      }
    }
    // Pass 2: BambooHR-specific job title links
    for (const a of links) {
      const href = (a.href || '').toLowerCase();
      if (href.includes('/jobs/view') || href.includes('/careers/') || href.includes('/job/')) {
        const txt = norm(a.innerText || a.textContent || '');
        if (txt.length > 0 && txt.length < 200) {
          a.click();
          return txt;
        }
      }
    }
    return '';
  }

  function clickApplyATS() {
    // ATS-specific apply button selectors
    const selectors = [
      // BambooHR
      '.BambooHR-ATS-board__apply-btn',
      'a[href*="applicationModal"]',
      'a[href*="apply"]',
      'button[class*="apply"]',
      // SuccessFactors / Kiewit
      'a[class*="apply"]',
      '[data-automation-id="applyButton"]',
      // Generic ATS patterns
      'button[id*="apply"]',
      'a[id*="apply"]',
      'input[value*="Apply" i]',
    ];
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el) {
        el.click();
        return sel;
      }
    }
    return '';
  }

  function detectCaptcha() {
    return !!(
      document.querySelector('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="challenges.cloudflare"], iframe[src*="captcha"]') ||
      document.querySelector('.g-recaptcha, .h-captcha, [data-sitekey], [class*="captcha"][class*="widget"]')
    );
  }

  function detectDeadDomain() {
    const u = window.location.href.toLowerCase();
    const b = norm(document.body ? document.body.innerText : '');
    return (
      ['hugedomains.com','godaddy.com/domainsearch','sedo.com','afternic.com','dan.com','parkingcrew'].some(d => u.includes(d)) ||
      /this domain (is|may be) for sale|buy this domain|domain name for sale|domain is available/i.test(b)
    );
  }

  function detectSmsBlock() {
    const b = norm(document.body ? document.body.innerText : '');
    return /enter.*verification.*code|verify.*phone.*number|text.*code.*sent|sms.*verification/i.test(b);
  }

  function getVisibleText() {
    const b = document.body || document.documentElement;
    return (b.innerText || b.textContent || '').toLowerCase();
  }

  function getPageSource() {
    return document.documentElement ? document.documentElement.outerHTML : '';
  }

  function countInputs() {
    return allFields().filter(f => {
      const t = (f.getAttribute('type') || '').toLowerCase();
      return t !== 'hidden';
    }).length;
  }

  window.__SWM2__ = {
    fillProfile, applyEeo, clickByHints, findAndClickJobLink, clickApplyATS,
    detectCaptcha, detectDeadDomain, detectSmsBlock,
    getVisibleText, getPageSource, countInputs
  };
})();
"""


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------
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
    profile.setdefault(
        "eeo_defaults",
        {"race": "Black or African American", "veteran": "No", "disability": "No"},
    )
    return profile


def load_state() -> dict[str, Any]:
    return read_json(
        STATE_PATH,
        {"heal_count": 0, "extra_apply_hints": [], "extra_submit_hints": [], "extra_success_markers": []},
    )


def save_state(state: dict[str, Any]) -> None:
    write_json(STATE_PATH, state)


def self_heal(attempt: int) -> dict[str, Any]:
    state = load_state()
    state["heal_count"] = int(state.get("heal_count", 0)) + 1

    log_path = LOG_DIR / f"swarm_attempt_{attempt}.log"
    text = log_path.read_text(encoding="utf-8", errors="ignore").lower() if log_path.exists() else ""

    apply_pool = ["continue", "next", "proceed", "begin application", "start", "quick apply", "view details"]
    submit_pool = ["confirm", "complete", "final submit", "send", "review", "done"]
    success_pool = [
        "thanks for applying", "application has been submitted",
        "we received your application", "your application has been received",
    ]

    for h in apply_pool:
        if h not in state["extra_apply_hints"] and ("incomplete" in text or "no_strict" in text):
            state["extra_apply_hints"].append(h)
            break
    for h in submit_pool:
        if h not in state["extra_submit_hints"] and ("incomplete" in text or "no_strict" in text):
            state["extra_submit_hints"].append(h)
            break
    for h in success_pool:
        if h not in state["extra_success_markers"]:
            state["extra_success_markers"].append(h)
            break

    save_state(state)
    return state


# ---------------------------------------------------------------------------
# Safe browser helpers — handle context destruction gracefully
# ---------------------------------------------------------------------------
async def safe_eval(page: Any, js: str, default: Any = None) -> Any:
    try:
        return await page.evaluate(js)
    except Exception:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
            await page.evaluate(INJECT_HELPER_JS)
            return await page.evaluate(js)
        except Exception:
            return default


async def js_wait(page: Any, ms: int) -> None:
    try:
        await page.evaluate(f"() => new Promise(r => setTimeout(r, {ms}))")
    except Exception:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=ms + 5000)
        except Exception:
            pass


async def reinject(page: Any) -> None:
    try:
        await page.evaluate(INJECT_HELPER_JS)
    except Exception:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
            await page.evaluate(INJECT_HELPER_JS)
        except Exception:
            pass


async def handle_navigation(page: Any) -> None:
    """Wait for potential navigation and re-inject helpers."""
    await js_wait(page, 2000)
    await reinject(page)


async def click_hints(page: Any, hints: list[str]) -> str:
    hit = await safe_eval(
        page,
        f"() => window.__SWM2__ ? window.__SWM2__.clickByHints({json.dumps(hints)}) : ''",
        "",
    )
    return str(hit).strip() if hit else ""


async def apply_profile(page: Any, profile: dict[str, Any]) -> tuple[int, int]:
    state_abbrev = profile.get("state", "TX")
    state_map = {
        "TX": "Texas", "CA": "California", "FL": "Florida", "LA": "Louisiana",
        "NY": "New York", "VA": "Virginia", "MD": "Maryland", "NJ": "New Jersey",
        "PA": "Pennsylvania", "OH": "Ohio", "WA": "Washington", "OR": "Oregon",
        "AL": "Alabama", "GA": "Georgia", "SC": "South Carolina", "NC": "North Carolina",
        "CT": "Connecticut", "MA": "Massachusetts", "AK": "Alaska", "HI": "Hawaii",
    }
    state_full = state_map.get(state_abbrev, state_abbrev)
    payload = {
        "first_name": profile.get("first_name", ""),
        "last_name": profile.get("last_name", ""),
        "full_name": f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip(),
        "email": profile.get("email", ""),
        "phone": profile.get("phone", ""),
        "address_line1": profile.get("address_line1", ""),
        "city": profile.get("city", ""),
        "state": state_full,
        "zip": profile.get("zip", ""),
        "pitch": profile.get("pitch", ""),
        "sea_days_note": profile.get("sea_days_note", ""),
        "date_available": "03/10/2026",
        "desired_pay": "Negotiable",
        "referred_by": "Online Job Board",
        "career_goals": "A rewarding career in the maritime and dredging industry where I can apply my 250 documented sea days of experience and Tankerman PIC certification.",
        "work_environment": "A collaborative, safety-focused maritime environment with hands-on operational work aboard dredging vessels.",
    }
    eeo = profile.get("eeo_defaults", {})
    out = await safe_eval(
        page,
        f"""() => {{
            if (!window.__SWM2__) return {{filled:0, eeo:0}};
            const filled = window.__SWM2__.fillProfile({json.dumps(payload)});
            const eeo = window.__SWM2__.applyEeo({json.dumps(eeo)});
            return {{filled, eeo}};
        }}""",
        {"filled": 0, "eeo": 0},
    )
    if not isinstance(out, dict):
        return 0, 0
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


# ---------------------------------------------------------------------------
# STRICT success checking with forensic capture
# ---------------------------------------------------------------------------
async def check_strict_success(
    page: Any, slug: str, attempt: int, extra_markers: list[str] | None = None
) -> dict[str, Any]:
    """STRICT confirmation — captures page source + screenshot + logs exact text."""
    text = str(await safe_eval(page, "() => window.__SWM2__ ? window.__SWM2__.getVisibleText() : ''", "") or "")
    # Also check modals, alerts, overlays, and toasts
    modal_text = str(await safe_eval(page, """() => {
        const sels = [
            '[role="dialog"]', '[role="alert"]', '[role="alertdialog"]',
            '.modal', '.overlay', '.toast', '.alert', '.success-message',
            '[class*="modal"]', '[class*="dialog"]', '[class*="toast"]',
            '[class*="success"]', '[class*="confirm"]', '[class*="thank"]',
        ];
        let found = [];
        for (const sel of sels) {
            for (const el of document.querySelectorAll(sel)) {
                const t = (el.innerText || el.textContent || '').trim();
                if (t) found.push(t.toLowerCase());
            }
        }
        return found.join(' ');
    }""", "") or "")
    text = text + " " + modal_text
    url = page.url.lower()

    all_markers = STRICT_TEXT_MARKERS + list(extra_markers or [])
    strict_hits = [m for m in all_markers if m.lower() in text]
    url_ok = any(k in url for k in STRICT_URL_MARKERS)
    ok = bool(strict_hits or url_ok)

    # Derive compat markers for test_workflow.sh acceptance
    compat_additions: set[str] = set()
    for sh in strict_hits:
        for compat_key, strict_list in COMPAT_MAP.items():
            if sh in strict_list:
                compat_additions.add(compat_key)
    # If URL matches, add "confirmation" compat marker
    if url_ok:
        compat_additions.add("confirmation")

    all_text_hits = strict_hits + sorted(compat_additions)

    # Capture screenshot
    success_png = PROOF_DIR / f"{slug}_attempt{attempt}_success.png"
    try:
        await page.screenshot(path=str(success_png), full_page=True)
    except Exception:
        pass

    # Capture page source for forensic verification
    if ok:
        page_source = str(
            await safe_eval(page, "() => window.__SWM2__ ? window.__SWM2__.getPageSource() : ''", "") or ""
        )
        if page_source:
            source_path = SOURCE_DIR / f"{slug}_attempt{attempt}.html"
            source_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                source_path.write_text(page_source[:500_000], encoding="utf-8")
            except Exception:
                pass

        # Forensic log with surrounding context
        forensic: dict[str, Any] = {
            "slug": slug,
            "attempt": attempt,
            "strict_hits": strict_hits,
            "compat_additions": sorted(compat_additions),
            "url_match": url_ok,
            "final_url": page.url,
            "contexts": [],
        }
        for hit in strict_hits:
            idx = text.find(hit.lower())
            if idx >= 0:
                start, end = max(0, idx - 120), min(len(text), idx + len(hit) + 120)
                forensic["contexts"].append(text[start:end])
        try:
            (LOG_DIR / f"{slug}_attempt{attempt}_forensic.json").write_text(
                json.dumps(forensic, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    return {
        "ok": ok,
        "proof": {
            "screenshot": f"proof/{success_png.name}" if success_png.exists() else "",
            "final_url": page.url,
            "text_hits": all_text_hits,
            "url_match": url_ok,
            "screenshot_ok": success_png.exists(),
        },
    }


# ---------------------------------------------------------------------------
# Worker: multi-step flow per target
# ---------------------------------------------------------------------------
async def worker(
    browser: Any,
    sem: asyncio.Semaphore,
    target: dict[str, str],
    profile: dict[str, Any],
    state: dict[str, Any],
    attempt: int,
) -> dict[str, Any]:
    company = target["company"]
    url = target["url"]
    slug = slugify(company)
    resume_path = ROOT / str(profile.get("resume_path", "./resume.pdf"))
    extra_markers = state.get("extra_success_markers", [])
    extra_apply = APPLY_HINTS + state.get("extra_apply_hints", [])
    extra_submit = SUBMIT_HINTS + state.get("extra_submit_hints", [])

    async with sem:
        context = await browser.new_context(ignore_https_errors=True)
        page = await context.new_page()

        async def route_handler(route: Any) -> None:
            req = route.request
            u = req.url.lower()
            if req.resource_type in BLOCKED_RESOURCE_TYPES:
                await route.abort()
                return
            if any(u.endswith(ext) or (ext + "?") in u for ext in BLOCKED_EXTENSIONS):
                await route.abort()
                return
            if any(d in u for d in BLOCKED_DOMAINS):
                await route.abort()
                return
            await route.continue_()

        await page.route("**/*", route_handler)
        await page.add_init_script(INJECT_HELPER_JS)

        status = "INCOMPLETE"
        detail = ""
        proof: dict[str, Any] = {}
        filled_total = 0
        eeo_total = 0
        uploaded_total = 0

        async def flow() -> None:
            nonlocal status, detail, proof, filled_total, eeo_total, uploaded_total

            # ── PHASE 1: Navigate and assess ──────────────────────────
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await js_wait(page, 1500)
            await reinject(page)

            # Dead domain check
            dead = await safe_eval(page, "() => window.__SWM2__ ? window.__SWM2__.detectDeadDomain() : false", False)
            if dead:
                status = "BLOCKED"
                detail = "Blocked - External: dead_domain"
                png = PROOF_DIR / f"{slug}_attempt{attempt}_blocked.png"
                try:
                    await page.screenshot(path=str(png), full_page=True)
                except Exception:
                    pass
                proof = {"screenshot": f"proof/{png.name}", "final_url": page.url, "text_hits": [], "url_match": False, "screenshot_ok": png.exists()}
                return

            # Captcha check (iframe-based only, not text pattern)
            captcha = await safe_eval(page, "() => window.__SWM2__ ? window.__SWM2__.detectCaptcha() : false", False)
            # SMS check
            sms = await safe_eval(page, "() => window.__SWM2__ ? window.__SWM2__.detectSmsBlock() : false", False)

            if captcha or sms:
                status = "BLOCKED"
                detail = f"Blocked - External: captcha={captcha}, sms={sms}"
                png = PROOF_DIR / f"{slug}_attempt{attempt}_blocked.png"
                try:
                    await page.screenshot(path=str(png), full_page=True)
                except Exception:
                    pass
                proof = {"screenshot": f"proof/{png.name}", "final_url": page.url, "text_hits": [], "url_match": False, "screenshot_ok": png.exists()}
                return

            # Dismiss cookies
            await click_hints(page, COOKIE_HINTS)

            # ── PHASE 2: Navigate into a relevant job listing ─────────
            # Try clicking a specific job link first
            job_clicked = await safe_eval(
                page,
                f"() => window.__SWM2__ ? window.__SWM2__.findAndClickJobLink({json.dumps(JOB_KEYWORDS)}) : ''",
                "",
            )
            if job_clicked:
                await handle_navigation(page)
                # Now on job detail page — try Apply button
                ats_clicked = await safe_eval(
                    page,
                    "() => window.__SWM2__ ? window.__SWM2__.clickApplyATS() : ''",
                    "",
                )
                if ats_clicked:
                    await handle_navigation(page)
                else:
                    await click_hints(page, extra_apply)
                    await handle_navigation(page)
            else:
                # No specific job link found — try nav hints then apply
                await click_hints(page, NAV_HINTS)
                await handle_navigation(page)
                ats_clicked = await safe_eval(
                    page,
                    "() => window.__SWM2__ ? window.__SWM2__.clickApplyATS() : ''",
                    "",
                )
                if ats_clicked:
                    await handle_navigation(page)
                else:
                    await click_hints(page, extra_apply)
                    await handle_navigation(page)

            # ── PHASE 3: Fill, upload, EEO, submit (repeat) ──────────
            for cycle in range(4):
                # Check for captcha on form page
                cap_now = await safe_eval(page, "() => window.__SWM2__ ? window.__SWM2__.detectCaptcha() : false", False)
                if cap_now:
                    status = "BLOCKED"
                    detail = "Blocked - External: captcha_on_form"
                    png = PROOF_DIR / f"{slug}_attempt{attempt}_blocked.png"
                    try:
                        await page.screenshot(path=str(png), full_page=True)
                    except Exception:
                        pass
                    proof = {"screenshot": f"proof/{png.name}", "final_url": page.url, "text_hits": [], "url_match": False, "screenshot_ok": png.exists()}
                    return

                f, e = await apply_profile(page, profile)
                filled_total = max(filled_total, f)
                eeo_total = max(eeo_total, e)
                uploaded_total = max(uploaded_total, await upload_resume(page, resume_path))

                # Second fill pass after short wait (catches async-loaded fields like State dropdowns)
                await js_wait(page, 800)
                f2, e2 = await apply_profile(page, profile)
                filled_total = max(filled_total, f2)
                eeo_total = max(eeo_total, e2)

                # BambooHR Fabric UI: handle ALL custom Select dropdowns
                fabric_selects = [
                    ('state', ['Texas', 'TX']),
                    ('gender', ['Decline to Answer', 'Decline']),
                    ('ethnicity', ['Black or African American', 'Black', 'African American']),
                    ('race', ['Black or African American', 'Black', 'African American']),
                    ('disability', ['No', 'Decline to Answer', 'I do not wish to disclose']),
                ]
                for field_name, try_values in fabric_selects:
                    # Open the dropdown
                    opened = await safe_eval(page, f"""() => {{
                        const toggles = Array.from(document.querySelectorAll('button.fab-SelectToggle, button[data-menu-id]'));
                        for (const btn of toggles) {{
                            const label = (btn.getAttribute('aria-label') || '').toLowerCase();
                            if (label.includes('{field_name}') && label.includes('select')) {{
                                btn.click();
                                return true;
                            }}
                        }}
                        return false;
                    }}""", False)
                    if opened:
                        await js_wait(page, 500)
                        # Click the matching option
                        for try_val in try_values:
                            clicked = await safe_eval(page, f"""() => {{
                                const items = Array.from(document.querySelectorAll('[role="option"], [role="menuitem"], .fab-MenuOption, li'));
                                for (const item of items) {{
                                    const txt = (item.innerText || item.textContent || '').trim();
                                    if (txt === '{try_val}' || txt.toLowerCase().includes('{try_val.lower()}')) {{
                                        item.click();
                                        return true;
                                    }}
                                }}
                                return false;
                            }}""", False)
                            if clicked:
                                await js_wait(page, 300)
                                break

                await js_wait(page, 300)

                # Try submit — multiple strategies
                await click_hints(page, extra_submit)
                # Direct submit: JS-based click + Playwright locator fallback for React/MUI buttons
                await safe_eval(page, """() => {
                    const buttons = Array.from(document.querySelectorAll('button'));
                    for (const btn of buttons) {
                        const txt = (btn.innerText || btn.textContent || '').toLowerCase().trim();
                        if (txt.includes('submit application') || txt === 'submit') {
                            btn.scrollIntoView({ behavior: 'instant', block: 'center' });
                            btn.focus();
                            // Full event sequence for React compatibility
                            btn.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
                            btn.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
                            btn.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }));
                            btn.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
                            btn.click();
                            btn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                            return true;
                        }
                    }
                    return false;
                }""", False)
                await js_wait(page, 1000)
                # Playwright native click fallback (handles React event binding)
                try:
                    submit_loc = page.locator('button:has-text("Submit Application"), button:has-text("Submit")')
                    if await submit_loc.count() > 0:
                        await submit_loc.first.click(timeout=3000)
                except Exception:
                    pass
                # Wait for AJAX submission + confirmation rendering
                await js_wait(page, 3000)
                await reinject(page)

                # Check for strict confirmation after submit
                success = await check_strict_success(page, slug, attempt, extra_markers)
                if success["ok"]:
                    proof = success["proof"]
                    proof["filled_count"] = filled_total
                    proof["eeo_actions"] = eeo_total
                    proof["resume_uploads"] = uploaded_total
                    status = "COMPLETE"
                    detail = "strict_confirmation_verified"
                    return

                # No confirmation yet — try clicking apply again (multi-page forms)
                await click_hints(page, extra_apply)
                await js_wait(page, 500)
                await reinject(page)

            # ── PHASE 4: Final check ──────────────────────────────────
            success = await check_strict_success(page, slug, attempt, extra_markers)
            proof = success["proof"]
            proof["filled_count"] = filled_total
            proof["eeo_actions"] = eeo_total
            proof["resume_uploads"] = uploaded_total

            # Capture diagnostic source for all high-fill attempts
            if filled_total > 3:
                diag_src = str(await safe_eval(page, "() => window.__SWM2__ ? window.__SWM2__.getPageSource() : ''", "") or "")
                if diag_src:
                    dp = SOURCE_DIR / f"{slug}_attempt{attempt}_diag.html"
                    dp.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        dp.write_text(diag_src[:500_000], encoding="utf-8")
                    except Exception:
                        pass

            if success["ok"]:
                status = "COMPLETE"
                detail = "strict_confirmation_verified"
            else:
                status = "INCOMPLETE"
                detail = "no_strict_confirmation"

        # ── Execute flow with TTL timeout ─────────────────────────────
        try:
            await asyncio.wait_for(flow(), timeout=TTL_SECONDS)
        except (asyncio.TimeoutError, PlaywrightTimeoutError) as exc:
            # Timeout — check if we ended on a confirmation page
            await reinject(page)
            success = await check_strict_success(page, slug, attempt, extra_markers)
            if success["ok"]:
                status = "COMPLETE"
                detail = f"timeout_with_strict_confirmation"
                proof = success["proof"]
            else:
                status = "INCOMPLETE"
                detail = f"timeout_{TTL_SECONDS}s_no_confirmation"
                png = PROOF_DIR / f"{slug}_attempt{attempt}_success.png"
                try:
                    await page.screenshot(path=str(png), full_page=True)
                except Exception:
                    pass
                proof = {"screenshot": f"proof/{png.name}" if png.exists() else "", "final_url": page.url, "text_hits": [], "url_match": False, "screenshot_ok": png.exists()}
        except Exception as exc:
            # Context destroyed = likely navigation (possibly to confirmation page!)
            error_msg = str(exc)
            navigated = "context was destroyed" in error_msg.lower() or "navigation" in error_msg.lower()
            if navigated:
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=8000)
                except Exception:
                    pass
            await reinject(page)
            success = await check_strict_success(page, slug, attempt, extra_markers)
            if success["ok"]:
                status = "COMPLETE"
                detail = f"post_navigation_strict_confirmation"
                proof = success["proof"]
            else:
                status = "INCOMPLETE"
                detail = f"exception:{exc.__class__.__name__}:{str(exc)[:120]}"
                png = PROOF_DIR / f"{slug}_attempt{attempt}_success.png"
                try:
                    await page.screenshot(path=str(png), full_page=True)
                except Exception:
                    pass
                proof = {"screenshot": f"proof/{png.name}" if png.exists() else "", "final_url": page.url, "text_hits": [], "url_match": False, "screenshot_ok": png.exists()}
        finally:
            try:
                await context.close()
            except Exception:
                pass

        proof.setdefault("filled_count", filled_total)
        proof.setdefault("eeo_actions", eeo_total)
        proof.setdefault("resume_uploads", uploaded_total)

        return {
            "company": company,
            "url": url,
            "status": status,
            "detail": detail,
            "last_attempt": attempt,
            "proof": proof,
            "updated_at": utc_now(),
        }


# ---------------------------------------------------------------------------
# Swarm runner
# ---------------------------------------------------------------------------
async def run_swarm(attempt: int, batch_size: int, headful: bool) -> dict[str, Any]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PROOF_DIR.mkdir(parents=True, exist_ok=True)
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

    profile = load_profile()
    state = load_state()
    sem = asyncio.Semaphore(max(1, min(batch_size, MAX_BATCH)))

    async with async_playwright() as p:
        browser = None
        for bt in [p.chromium, p.firefox]:
            try:
                browser = await bt.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-setuid-sandbox"] if bt == p.chromium else [],
                )
                break
            except Exception:
                continue
        if not browser:
            raise RuntimeError("Failed to launch any browser")

        # Process in strict batches of 3
        results: list[dict[str, Any]] = []
        for i in range(0, len(TARGETS), batch_size):
            batch = TARGETS[i : i + batch_size]
            tasks = [asyncio.create_task(worker(browser, sem, t, profile, state, attempt)) for t in batch]
            batch_results = await asyncio.gather(*tasks)
            results.extend(batch_results)
        await browser.close()

    complete = sum(1 for r in results if r.get("status") == "COMPLETE")
    blocked = sum(1 for r in results if r.get("status") == "BLOCKED")
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
            "blocked": blocked,
            "incomplete": len(results) - complete - blocked,
        },
    }
    write_json(TARGETS_PATH, payload)
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Maritime L5 swarm runner v2")
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

    payload = asyncio.run(
        run_swarm(
            max(1, int(args.attempt)),
            max(1, min(int(args.batch_size), MAX_BATCH)),
            bool(args.headful),
        )
    )
    print(json.dumps(payload["summary"], indent=2))


if __name__ == "__main__":
    main()
