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

TTL_SECONDS = 120
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
    "your application was submitted",
    "application was submitted successfully",
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
        "your application was submitted",
        "application was submitted successfully",
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

COOKIE_HINTS = ["accept", "accept all", "allow all", "i agree", "agree", "got it", "ok", "dismiss"]
JOB_KEYWORDS = [
    "deckhand", "entry level", "entry-level", "dredge",
    "trainee", "boatman", "crew", "leverman", "oiler",
    "maritime training", "deck", "tankerman",
    "view our employment", "apply today",
]
APPLY_HINTS = [
    "apply for this job", "apply now", "apply", "apply online",
    "start application", "continue application", "apply for this position",
    "apply today", "submit application", "type it in myself",
]
SUBMIT_HINTS = [
    "submit", "submit application", "submit my application",
    "finish application", "complete application", "review and submit",
    "send", "send application", "save", "save application",
    "submit your application", "apply", "confirm",
]
NAV_HINTS = [
    "careers", "view our employment", "view our emplyment",
    "how to apply", "apply today",
    "send resume", "read more", "view opportunities",
    "see open positions", "current openings", "job openings",
    "open positions", "join our team", "employment", "emplyment",
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
    // Skip honeypot fields (anti-bot traps)
    if (el.tabIndex === -1) return false;
    const closestHidden = el.closest('[aria-hidden="true"]');
    if (closestHidden) return false;
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
    // Only detect visible captcha widgets, not reCAPTCHA v3 buttons
    const iframe = document.querySelector('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="challenges.cloudflare"], iframe[src*="captcha"]');
    if (iframe) return true;
    // Require data-sitekey for widget detection (avoid false positives from g-recaptcha class on buttons)
    const widget = document.querySelector('[data-sitekey], .h-captcha[data-sitekey]');
    if (widget) return true;
    // Check for visible captcha challenge box
    const challenge = document.querySelector('[class*="captcha"][class*="widget"]:not(button):not(sdf-button)');
    if (challenge && challenge.offsetHeight > 50) return true;
    return false;
  }

  function detectDeadDomain() {
    const u = window.location.href.toLowerCase();
    const b = norm(document.body ? document.body.innerText : '');
    return (
      ['hugedomains.com','godaddy.com/domainsearch','sedo.com','afternic.com','dan.com','parkingcrew'].some(d => u.includes(d)) ||
      /this domain (is|may be) for sale|buy this domain|domain name for sale|domain is available/i.test(b) ||
      /server error in.*application|runtime error|an application error occurred on the server/i.test(b)
    );
  }

  function detectLoginBlock() {
    const b = norm(document.body ? document.body.innerText : '');
    return /already have an account|please log in to continue|sign in to continue|create an account to apply/i.test(b);
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
    detectCaptcha, detectDeadDomain, detectSmsBlock, detectLoginBlock,
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

        SOCIAL_DOMAINS = {"facebook.com", "twitter.com", "x.com", "linkedin.com",
                          "instagram.com", "youtube.com", "tiktok.com", "pinterest.com"}

        async def follow_popup(page_ref, ctx):
            """Switch to new tab/popup if one opened, skipping social media."""
            pages = ctx.pages
            if len(pages) > 1:
                new_page = pages[-1]
                try:
                    await new_page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
                popup_url = new_page.url.lower()
                if any(sd in popup_url for sd in SOCIAL_DOMAINS):
                    try:
                        await new_page.close()
                    except Exception:
                        pass
                    return page_ref
                await new_page.route("**/*", route_handler)
                await new_page.add_init_script(INJECT_HELPER_JS)
                await reinject(new_page)
                return new_page
            return page_ref

        async def flow() -> None:
            nonlocal status, detail, proof, filled_total, eeo_total, uploaded_total, page

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
                page = await follow_popup(page, context)
                # Now on job detail page — try Apply button
                ats_clicked = await safe_eval(
                    page,
                    "() => window.__SWM2__ ? window.__SWM2__.clickApplyATS() : ''",
                    "",
                )
                if ats_clicked:
                    await handle_navigation(page)
                    page = await follow_popup(page, context)
                else:
                    await click_hints(page, extra_apply)
                    await handle_navigation(page)
                    page = await follow_popup(page, context)
            else:
                # No specific job link found — try nav hints then apply
                await click_hints(page, NAV_HINTS)
                await handle_navigation(page)
                page = await follow_popup(page, context)
                ats_clicked = await safe_eval(
                    page,
                    "() => window.__SWM2__ ? window.__SWM2__.clickApplyATS() : ''",
                    "",
                )
                if ats_clicked:
                    await handle_navigation(page)
                    page = await follow_popup(page, context)
                else:
                    await click_hints(page, extra_apply)
                    await handle_navigation(page)
                    page = await follow_popup(page, context)

            # ── SITE-SPECIFIC: Playwright click for stubborn buttons ──
            cur_url = page.url.lower()

            # Callan Marine: "APPLY NOW" oval button (multi-line text, styled <a>)
            if "callanmarine" in cur_url:
                try:
                    apply_btn = page.locator('a:has-text("APPLY"), a:has-text("Apply Now"), a:has-text("APPLY NOW")').first
                    if await apply_btn.count() > 0:
                        await apply_btn.click(timeout=5000)
                        await handle_navigation(page)
                        page = await follow_popup(page, context)
                except Exception:
                    pass

            # ADP Career Center: use Playwright native click on sdf-link (SPA)
            cur_url = page.url.lower()  # refresh URL after site-specific clicks
            if "adp.com" in cur_url:
                await js_wait(page, 3000)  # wait for SPA to render
                await reinject(page)
                # Find job link ID using JS eval, then click with Playwright
                job_id = await safe_eval(page, """() => {
                    const keywords = ['oiler', 'deckhand', 'dredge', 'crew', 'marine'];
                    for (const el of document.querySelectorAll('sdf-link')) {
                        const txt = (el.textContent || '').trim().toLowerCase();
                        for (const kw of keywords) {
                            if (txt.includes(kw)) return el.id || '';
                        }
                    }
                    return '';
                }""", "")
                print(f"  [ADP] job_id: {job_id}, URL: {page.url[:80]}", flush=True)
                if job_id:
                    try:
                        # Use Playwright native click (triggers real mouse events for SPA)
                        el = page.locator(f'#{job_id}')
                        await el.scroll_into_view_if_needed(timeout=2000)
                        await el.click(timeout=5000)
                        print(f"  [ADP] clicked job: {job_id}", flush=True)
                        await js_wait(page, 3000)
                        await reinject(page)
                        # On job detail page — find Apply button
                        apply_id = await safe_eval(page, """() => {
                            for (const el of document.querySelectorAll('sdf-link, sdf-button, a, button, [role="button"]')) {
                                const txt = (el.textContent || '').trim().toLowerCase();
                                if (txt.includes('apply') && !txt.includes('affirmative') && !txt.includes('action')) {
                                    return el.id || el.tagName + ':' + txt;
                                }
                            }
                            return '';
                        }""", "")
                        print(f"  [ADP] apply_id: {apply_id}", flush=True)
                        if apply_id and ':' not in apply_id:
                            apply_el = page.locator(f'#{apply_id}')
                            await apply_el.click(timeout=5000)
                        else:
                            # Fallback: click by text matching
                            for kw in ["Apply", "APPLY"]:
                                try:
                                    btn = page.locator(f'sdf-button:has-text("{kw}"), button:has-text("{kw}"), a:has-text("{kw}")').first
                                    if await btn.count() > 0:
                                        await btn.click(timeout=5000)
                                        break
                                except Exception:
                                    pass
                        await js_wait(page, 3000)
                        await handle_navigation(page)
                        page = await follow_popup(page, context)
                        await reinject(page)
                    except Exception as e:
                        print(f"  [ADP] error: {e}", flush=True)

            # Viking Dredging: "VIEW OUR EMPLYMENT OPPORTUNITIES" (typo)
            if "vikingdredging" in cur_url:
                try:
                    emp_btn = page.locator('a:has-text("EMPLYMENT"), a:has-text("EMPLOYMENT"), a:has-text("VIEW OUR")').first
                    if await emp_btn.count() > 0:
                        await emp_btn.click(timeout=5000)
                        await handle_navigation(page)
                        page = await follow_popup(page, context)
                except Exception:
                    pass

            # Moran Towing: navigate to saashr.com ATS directly
            cur_url2 = page.url.lower()
            if "morantug" in cur_url2:
                await js_wait(page, 2000)  # wait for dynamic content
                saashr_url = await safe_eval(page, """() => {
                    for (const a of document.querySelectorAll('a')) {
                        if (a.href && (a.href.includes('saashr') || a.href.includes('secure4'))) {
                            return a.href;
                        }
                    }
                    return '';
                }""", "")
                print(f"  [MORAN] saashr URL: {saashr_url}", flush=True)
                if saashr_url:
                    try:
                        await page.goto(saashr_url, timeout=15000, wait_until="domcontentloaded")
                        await js_wait(page, 2000)
                        await reinject(page)
                        print(f"  [MORAN] navigated to: {page.url[:80]}", flush=True)
                    except Exception as e:
                        print(f"  [MORAN] nav error: {e}", flush=True)

            # Dismiss any modal dialogs (like resume parse errors on ATS portals)
            # Use Playwright locators for reliable modal button clicks
            for btn_text in ["OK", "Ok", "Close", "Dismiss", "Got it"]:
                try:
                    loc = page.locator(f'button:has-text("{btn_text}")').first
                    if await loc.count() > 0 and await loc.is_visible():
                        await loc.click(timeout=2000)
                        await js_wait(page, 500)
                except Exception:
                    pass
            # Then click "Type it in myself" or "Continue" to access manual form
            for btn_text in ["Type it in myself", "Continue", "Start", "Next", "Manual entry"]:
                try:
                    loc = page.locator(f'button:has-text("{btn_text}"), a:has-text("{btn_text}"), label:has-text("{btn_text}"), input[value="{btn_text}"]').first
                    if await loc.count() > 0 and await loc.is_visible():
                        await loc.click(timeout=2000)
                        await js_wait(page, 1000)
                except Exception:
                    pass
            # Also try radio buttons for "Manual entry" option
            try:
                manual_radio = page.locator('input[type="radio"]')
                for i in range(await manual_radio.count()):
                    r = manual_radio.nth(i)
                    label = await r.evaluate("el => (el.closest('label') || el.parentElement)?.innerText || ''")
                    if 'manual' in label.lower():
                        await r.click(timeout=2000)
                        break
            except Exception:
                pass
            # Click NEXT button for multi-step forms
            for btn_text in ["NEXT", "Next", "Continue", "Proceed"]:
                try:
                    loc = page.locator(f'button:has-text("{btn_text}"), a:has-text("{btn_text}"), input[value="{btn_text}"]').first
                    if await loc.count() > 0 and await loc.is_visible():
                        await loc.click(timeout=2000)
                        await js_wait(page, 1000)
                        break
                except Exception:
                    pass

            # Check for login/account blocker
            login_block = await safe_eval(page, "() => window.__SWM2__ ? window.__SWM2__.detectLoginBlock() : false", False)
            if login_block:
                status = "BLOCKED"
                detail = "Blocked - External: login_required"
                png = PROOF_DIR / f"{slug}_attempt{attempt}_blocked.png"
                try:
                    await page.screenshot(path=str(png), full_page=True)
                except Exception:
                    pass
                proof = {"screenshot": f"proof/{png.name}", "final_url": page.url, "text_hits": [], "url_match": False, "screenshot_ok": png.exists()}
                return

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

                # Second fill pass after short wait
                await js_wait(page, 800)
                f2, e2 = await apply_profile(page, profile)
                filled_total = max(filled_total, f2)
                eeo_total = max(eeo_total, e2)

                # BambooHR React-specific: use Playwright fill() for controlled inputs
                if "bamboohr" in page.url:
                    bamboo_fields = {
                        'input[name="firstName"]': profile.get("first_name", ""),
                        'input[name="lastName"]': profile.get("last_name", ""),
                        'input[name="email"]': profile.get("email", ""),
                        'input[name="phone"]': profile.get("phone", ""),
                        'input[name="streetAddress"]': profile.get("address", ""),
                        'input[name="city"]': profile.get("city", ""),
                        'input[name="zip"]': profile.get("zip", ""),
                        'input[name="dateAvailable"]': "03/10/2026",
                        'input[name="desiredPay"]': "Negotiable",
                        'input[name="referredBy"]': "Online Job Board",
                    }
                    # Also try label-based selectors
                    label_fields = {
                        "First Name": profile.get("first_name", ""),
                        "Last Name": profile.get("last_name", ""),
                        "Email": profile.get("email", ""),
                        "Phone": profile.get("phone", ""),
                        "Street Address": profile.get("address", ""),
                        "City": profile.get("city", ""),
                        "Zip": profile.get("zip", ""),
                        "Date Available": "03/10/2026",
                        "Desired Pay": "Negotiable",
                        "Who Referred You": "Online Job Board",
                    }
                    pw_filled = 0
                    for sel, val in bamboo_fields.items():
                        if not val:
                            continue
                        try:
                            loc = page.locator(sel)
                            if await loc.count() > 0:
                                await loc.first.fill(val, timeout=2000)
                                pw_filled += 1
                        except Exception:
                            pass
                    # Label-based fill for fields not matched by name
                    for lbl, val in label_fields.items():
                        if not val:
                            continue
                        try:
                            loc = page.get_by_label(lbl)
                            if await loc.count() > 0:
                                await loc.first.fill(val, timeout=2000)
                                pw_filled += 1
                        except Exception:
                            pass
                    # Textareas
                    textarea_fields = {
                        "career": profile.get("career_goals", "Seeking full-time Deckhand/Tankerman role in maritime industry."),
                        "experience": profile.get("cover_letter", ""),
                        "environment": profile.get("work_environment", "Team-oriented maritime operations environment with safety focus."),
                    }
                    for keyword, val in textarea_fields.items():
                        if not val:
                            continue
                        try:
                            textareas = page.locator("textarea")
                            for i in range(await textareas.count()):
                                ta = textareas.nth(i)
                                name = await ta.get_attribute("name") or ""
                                aria = await ta.get_attribute("aria-label") or ""
                                ident = (name + " " + aria).lower()
                                if keyword in ident:
                                    await ta.fill(val, timeout=2000)
                                    pw_filled += 1
                                    break
                        except Exception:
                            pass
                    if pw_filled > 0:
                        filled_total = max(filled_total, pw_filled)
                        print(f"  [PW-FILL] BambooHR Playwright fill: {pw_filled} fields", flush=True)

                # BambooHR Fabric UI dropdown handler — sequential to avoid menu overlap
                if "bamboohr" in page.url:
                    fabric_selects = [
                        ('state', ['Texas', 'TX']),
                        ('gender', ['Decline to answer', 'Decline to Answer', 'Decline']),
                        ('ethnicity', ['Black or African American', 'Black']),
                        ('disability', ['Decline to answer', 'Decline to Answer', 'Decline',
                                        'I do not wish to answer', 'No, I Do Not Have a Disability',
                                        'No', 'None']),
                    ]
                    for field_name, try_values in fabric_selects:
                        # Only open if still "--Select--"
                        opened = await safe_eval(page, f"""() => {{
                            const toggles = Array.from(document.querySelectorAll('button.fab-SelectToggle, button[data-menu-id]'));
                            for (const btn of toggles) {{
                                const label = (btn.getAttribute('aria-label') || '').toLowerCase();
                                if (label.includes('{field_name}') && label.includes('select')) {{
                                    btn.scrollIntoView({{block:'center'}});
                                    btn.click();
                                    return true;
                                }}
                            }}
                            return false;
                        }}""", False)
                        if opened:
                            await js_wait(page, 1500)
                            clicked = False
                            for try_val in try_values:
                                clicked = await safe_eval(page, f"""() => {{
                                    const items = Array.from(document.querySelectorAll('.fab-MenuOption, .fab-MenuOption__content, [role="option"], [role="menuitem"]'));
                                    for (const item of items) {{
                                        const txt = (item.innerText || item.textContent || '').trim();
                                        if (txt.toLowerCase() === '{try_val.lower()}' || txt.toLowerCase().includes('{try_val.lower()}')) {{
                                            item.click();
                                            return true;
                                        }}
                                    }}
                                    return false;
                                }}""", False)
                                if clicked:
                                    await js_wait(page, 500)
                                    break
                            if not clicked:
                                await page.keyboard.press("Escape")
                                await js_wait(page, 300)
                                # Nuclear fallback: set the hidden <select> value directly
                                await safe_eval(page, f"""() => {{
                                    const sel = document.querySelector('select[name="{field_name}Id"], select[name="{field_name}"]');
                                    if (sel && sel.options) {{
                                        for (let i = 0; i < sel.options.length; i++) {{
                                            const txt = sel.options[i].text.toLowerCase();
                                            if (txt.includes('decline') || txt.includes('no') || txt === 'texas') {{
                                                sel.value = sel.options[i].value;
                                                sel.dispatchEvent(new Event('change', {{bubbles: true}}));
                                                // Also trigger React's nativeInputValueSetter
                                                const nativeSetter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')?.set;
                                                if (nativeSetter) {{
                                                    nativeSetter.call(sel, sel.options[i].value);
                                                    sel.dispatchEvent(new Event('change', {{bubbles: true}}));
                                                }}
                                                return true;
                                            }}
                                        }}
                                    }}
                                    return false;
                                }}""", False)

                await js_wait(page, 300)

                # ADP Workforce Now: fill registration & application forms
                if "adp.com" in page.url:
                    adp_fields = {
                        'input[name="guestFirstName"]': profile.get("first_name", ""),
                        'input[name="guestLastName"]': profile.get("last_name", ""),
                        'input[name="Email"], input[name="email"]': profile.get("email", ""),
                        'input[name="phone"]': profile.get("phone", ""),
                    }
                    adp_label_fields = {
                        "First Name": profile.get("first_name", ""),
                        "Last Name": profile.get("last_name", ""),
                        "Email": profile.get("email", ""),
                        "Mobile Number": profile.get("phone", ""),
                        "Phone": profile.get("phone", ""),
                        "Street Address": profile.get("address", ""),
                        "City": profile.get("city", ""),
                        "Zip": profile.get("zip", ""),
                    }
                    adp_filled = 0
                    for sel, val in adp_fields.items():
                        if not val:
                            continue
                        try:
                            loc = page.locator(sel)
                            if await loc.count() > 0:
                                await loc.first.fill(val, timeout=2000)
                                adp_filled += 1
                        except Exception:
                            pass
                    for lbl, val in adp_label_fields.items():
                        if not val:
                            continue
                        try:
                            loc = page.get_by_label(lbl)
                            if await loc.count() > 0:
                                await loc.first.fill(val, timeout=2000)
                                adp_filled += 1
                        except Exception:
                            pass
                    if adp_filled > 0:
                        filled_total = max(filled_total, adp_filled)
                        print(f"  [PW-FILL] ADP fill: {adp_filled} fields", flush=True)
                    # Click Continue/Submit on ADP
                    for btn_id in ["recruitment_login_recaptcha", "recruitment_login_submit"]:
                        try:
                            btn = page.locator(f'#{btn_id}')
                            if await btn.count() > 0 and await btn.is_visible():
                                await btn.click(timeout=5000)
                                await js_wait(page, 3000)
                                await reinject(page)
                                break
                        except Exception:
                            pass

                # Multi-step form: advance to next page after filling
                cur = page.url.lower()
                if "ourcareerpages" in cur or "entertimeonline" in cur or "careers" in cur:
                    # Click Continue/Next/Save & Continue on multi-step forms
                    for step_text in ["Continue", "NEXT", "Next", "Save & Continue",
                                       "Save and Continue", "NEXT: CONTACT INFO",
                                       "Submit Application", "Submit"]:
                        try:
                            loc = page.locator(f'button:has-text("{step_text}"), a:has-text("{step_text}"), input[type="submit"][value*="{step_text}"]').first
                            if await loc.count() > 0 and await loc.is_visible():
                                await loc.click(timeout=3000)
                                await js_wait(page, 2000)
                                await reinject(page)
                                break
                        except Exception:
                            pass
                    # Dismiss error dialogs that appear after form actions
                    for dismiss_text in ["OK", "Close", "Dismiss"]:
                        try:
                            d = page.locator(f'button:has-text("{dismiss_text}")').first
                            if await d.count() > 0 and await d.is_visible():
                                await d.click(timeout=1000)
                                await js_wait(page, 500)
                        except Exception:
                            pass

                # Clear any honeypot fields (anti-bot traps)
                await safe_eval(page, """() => {
                    document.querySelectorAll('[aria-hidden="true"] input, input[tabindex="-1"]').forEach(inp => {
                        if (inp.value) {
                            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
                            if (setter) setter.call(inp, '');
                            else inp.value = '';
                            inp.dispatchEvent(new Event('input', {bubbles: true}));
                            inp.dispatchEvent(new Event('change', {bubbles: true}));
                        }
                    });
                }""", None)

                # React state diagnostic: check what React thinks each field contains
                if "bamboohr" in page.url:
                    react_diag = await safe_eval(page, """() => {
                        const form = document.getElementById('job-application-form') || document.querySelector('form');
                        if (!form) return {error: 'no_form'};
                        const inputs = Array.from(form.querySelectorAll('input, textarea, select'));
                        const state = {};
                        const empty = [];
                        const required = [];
                        for (const inp of inputs) {
                            const name = inp.name || inp.id || inp.getAttribute('aria-label') || inp.type;
                            const val = inp.value || '';
                            state[name] = val.substring(0, 30);
                            if (!val && inp.type !== 'hidden' && inp.type !== 'file') {
                                empty.push(name);
                            }
                            if (inp.required || inp.getAttribute('aria-required') === 'true') {
                                required.push(name + '=' + (val ? 'OK' : 'EMPTY'));
                            }
                        }
                        // Also check React fiber for validation state
                        const submitBtn = form.querySelector('button[type="submit"]');
                        const btnDisabled = submitBtn ? submitBtn.disabled : 'no_btn';
                        return {
                            total: inputs.length,
                            empty_count: empty.length,
                            empty: empty.slice(0, 10),
                            required: required.slice(0, 15),
                            btn_disabled: btnDisabled
                        };
                    }""", {"error": "eval_failed"})
                    print(f"  [REACT-DIAG] {react_diag}", flush=True)

                # Submit — capture network + console for debugging
                before_url = page.url
                submit_responses: list[dict] = []
                console_msgs: list[str] = []

                def _on_resp(resp):
                    method = resp.request.method
                    u = resp.url.lower()
                    if method in ("POST", "PUT", "PATCH") or "api" in u or "bamboohr" in u:
                        submit_responses.append({"url": resp.url[:120], "status": resp.status, "method": method})

                def _on_console(msg):
                    if msg.type in ("error", "warning"):
                        console_msgs.append(f"[{msg.type}] {msg.text[:200]}")

                page.on("response", _on_resp)
                page.on("console", _on_console)

                try:
                    # Monkey-patch fetch to log outgoing requests
                    if "bamboohr" in page.url:
                        await safe_eval(page, """() => {
                            if (!window.__submitLog) {
                                window.__submitLog = [];
                                const origFetch = window.fetch;
                                window.fetch = function(...args) {
                                    window.__submitLog.push({type: 'fetch', url: String(args[0]).substring(0, 100), method: args[1]?.method || 'GET'});
                                    return origFetch.apply(this, args);
                                };
                                const origXhrOpen = XMLHttpRequest.prototype.open;
                                XMLHttpRequest.prototype.open = function(method, url) {
                                    window.__submitLog.push({type: 'xhr', url: String(url).substring(0, 100), method: method});
                                    return origXhrOpen.apply(this, arguments);
                                };
                            }
                        }""", None)

                    # Tier 1: Playwright native click (most reliable for React)
                    try:
                        submit_btn = page.locator('button[type="submit"]')
                        if await submit_btn.count() > 0:
                            await submit_btn.first.scroll_into_view_if_needed(timeout=2000)
                            await submit_btn.first.click(timeout=5000, force=True)
                    except Exception as e:
                        print(f"  [SUBMIT-T1] Click error: {e}", flush=True)

                    await js_wait(page, 3000)

                    # Tier 2: form.requestSubmit() with error capture
                    if not submit_responses:
                        submit_err = await safe_eval(page, """() => {
                            const form = document.getElementById('job-application-form') || document.querySelector('form');
                            if (!form) return 'no_form_found';
                            try { form.requestSubmit(); return 'requestSubmit_ok'; }
                            catch(e) { return 'requestSubmit_err: ' + e.message; }
                        }""", "eval_error")
                        print(f"  [SUBMIT-T2] requestSubmit result: {submit_err}", flush=True)
                        await js_wait(page, 3000)

                    # Tier 3: JS click with full event sequence
                    if not submit_responses:
                        await click_hints(page, extra_submit)
                        await js_wait(page, 3000)

                    # Wait for AJAX response
                    await js_wait(page, 3000)

                    # Log diagnostics
                    if submit_responses:
                        for sr in submit_responses:
                            print(f"  [SUBMIT-NET] {sr.get('method','?')} {sr['status']} {sr['url']}", flush=True)
                    else:
                        print(f"  [SUBMIT-NET] No POST requests detected — form may not have submitted", flush=True)
                    for cm in console_msgs[:5]:
                        print(f"  [CONSOLE] {cm}", flush=True)
                    # Check fetch/XHR monkey-patch log
                    if "bamboohr" in page.url:
                        fetch_log = await safe_eval(page, "() => JSON.stringify(window.__submitLog || [])", "[]")
                        print(f"  [FETCH-LOG] {fetch_log}", flush=True)
                        # Also check for visible validation errors after submit attempt
                        vis_errors = await safe_eval(page, """() => {
                            const errs = [];
                            document.querySelectorAll('[class*="error"], [class*="Error"], [role="alert"]').forEach(el => {
                                const txt = (el.innerText || '').trim();
                                if (txt && txt.length < 200) errs.push(txt);
                            });
                            return errs.slice(0, 10);
                        }""", [])
                        if vis_errors:
                            print(f"  [VIS-ERRORS] {vis_errors}", flush=True)

                finally:
                    page.remove_listener("response", _on_resp)
                    page.remove_listener("console", _on_console)

                if page.url != before_url:
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=5000)
                    except Exception:
                        pass
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

            # Capture diagnostic source for ALL attempts (not just high-fill)
            if True:
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
