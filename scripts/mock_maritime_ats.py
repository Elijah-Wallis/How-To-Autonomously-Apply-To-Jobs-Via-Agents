#!/usr/bin/env python3
"""Local mock maritime ATS suite for deterministic acceptance testing."""

from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


MOCK_COMPANIES = [
    {
        "company": "Curtin Maritime",
        "slug": "curtin-maritime",
        "mode": "standard",
        "job_title": "Deckhand / Dredge Crew Trainee",
    },
    {
        "company": "Great Lakes Dredge & Dock",
        "slug": "great-lakes-dredge-dock",
        "mode": "captcha",
        "job_title": "Dredge Deckhand",
    },
    {
        "company": "Weeks Marine",
        "slug": "weeks-marine",
        "mode": "multi",
        "job_title": "Entry-Level Marine Construction Deckhand",
    },
    {
        "company": "Manson Construction",
        "slug": "manson-construction",
        "mode": "multi",
        "job_title": "Tankerman Trainee / Deck Crew",
    },
    {
        "company": "Callan Marine",
        "slug": "callan-marine",
        "mode": "standard",
        "job_title": "Deckhand - Gulf Coast Dredging",
    },
    {
        "company": "Cashman Dredging",
        "slug": "cashman-dredging",
        "mode": "standard",
        "job_title": "Marine Crew / Deck Operations",
    },
    {
        "company": "Viking Dredging",
        "slug": "viking-dredging",
        "mode": "standard",
        "job_title": "Inland Dredge Deckhand",
    },
    {
        "company": "Muddy Water Dredging",
        "slug": "muddy-water-dredging",
        "mode": "multi",
        "job_title": "River Dredge Crew Member",
    },
    {
        "company": "Orion Government Services",
        "slug": "orion-government-services",
        "mode": "sms",
        "job_title": "Marine Operations Assistant",
    },
    {
        "company": "Moran Towing",
        "slug": "moran-towing",
        "mode": "standard",
        "job_title": "Deckhand - Tug and Barge",
    },
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the mock maritime ATS server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--targets-out")
    return parser.parse_args()


def build_targets(host: str, port: int) -> list[dict[str, str]]:
    return [
        {
            "company": company["company"],
            "url": f"http://{host}:{port}/company/{company['slug']}/jobs",
        }
        for company in MOCK_COMPANIES
    ]


def page_template(title: str, body: str) -> bytes:
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 32px auto; max-width: 980px; line-height: 1.45; color: #102030; }}
    .card {{ border: 1px solid #cbd5e1; border-radius: 12px; padding: 20px; margin: 18px 0; }}
    .actions a, .actions button {{ display: inline-block; margin-right: 10px; margin-top: 12px; }}
    label {{ display: block; margin: 10px 0 4px; font-weight: 600; }}
    input, textarea, select {{ width: 100%; max-width: 720px; padding: 10px; }}
    .inline {{ display: inline-block; margin-right: 16px; }}
  </style>
</head>
<body>
{body}
</body>
</html>
"""
    return html.encode("utf-8")


def company_by_slug(slug: str) -> dict[str, str] | None:
    for company in MOCK_COMPANIES:
        if company["slug"] == slug:
            return company
    return None


def jobs_page(company: dict[str, str]) -> bytes:
    if company["mode"] == "captcha":
        body = f"""
<h1>{company['company']} Careers</h1>
<div class="card">
  <h2>{company['job_title']}</h2>
  <p>Please verify you are human before continuing to the maritime hiring portal.</p>
  <iframe src="/captcha/widget" title="captcha widget"></iframe>
</div>
"""
        return page_template(f"{company['company']} Careers", body)

    if company["mode"] == "sms":
        body = f"""
<h1>{company['company']} Careers</h1>
<div class="card">
  <h2>{company['job_title']}</h2>
  <p>Enter the verification code sent by SMS to continue your application.</p>
  <p>This mock employer simulates a manual verification checkpoint.</p>
</div>
"""
        return page_template(f"{company['company']} Careers", body)

    body = f"""
<h1>{company['company']} Maritime Careers</h1>
<div class="card">
  <h2>Current Openings</h2>
  <p>We are hiring for Gulf Coast rotational maritime operations, dredge crew, tug and barge support, and entry-level deck roles.</p>
  <p><a href="/company/{company['slug']}/job">{company['job_title']}</a></p>
</div>
"""
    return page_template(f"{company['company']} Careers", body)


def job_page(company: dict[str, str]) -> bytes:
    body = f"""
<h1>{company['job_title']}</h1>
<div class="card">
  <p>{company['company']} is seeking immediate-availability maritime talent for deck, dredge, towing, and tankerman support operations.</p>
  <p>This mock role favors candidates with RFPNW watchstanding, line handling, mooring, towing, STCW, and tank barge cargo-transfer exposure.</p>
  <div class="actions">
    <a href="/company/{company['slug']}/apply">Apply for this job</a>
  </div>
</div>
"""
    return page_template(company["job_title"], body)


def application_form(company: dict[str, str], step: int) -> bytes:
    multi = company["mode"] == "multi"
    if multi and step == 2:
        body = f"""
<h1>{company['company']} application review</h1>
<div class="card">
  <form method="post" action="/company/{company['slug']}/submit">
    <label for="career_goals">Career goals</label>
    <textarea id="career_goals" name="career_goals" rows="4"></textarea>

    <label for="work_environment">Ideal work environment</label>
    <textarea id="work_environment" name="work_environment" rows="4"></textarea>

    <label for="race">Race / Ethnicity</label>
    <select id="race" name="race">
      <option value="">Select</option>
      <option>Black or African American</option>
      <option>Decline to Answer</option>
    </select>

    <label for="veteran">Veteran Status</label>
    <select id="veteran" name="veteran">
      <option value="">Select</option>
      <option>No</option>
      <option>Decline to Answer</option>
    </select>

    <label for="disability">Disability Status</label>
    <select id="disability" name="disability">
      <option value="">Select</option>
      <option>No</option>
      <option>Decline to Answer</option>
    </select>

    <div class="actions">
      <button type="submit">Submit Application</button>
    </div>
  </form>
</div>
"""
        return page_template(f"{company['company']} application step 2", body)

    submit_text = "Continue" if multi else "Submit Application"
    action = f"/company/{company['slug']}/apply?step=2" if multi else f"/company/{company['slug']}/submit"
    body = f"""
<h1>{company['company']} application</h1>
<div class="card">
  <p>Complete the maritime application below. This mock form is safe and used only for automated acceptance testing.</p>
  <form method="post" enctype="multipart/form-data" action="{action}">
    <label for="first_name">First Name</label>
    <input id="first_name" name="first_name" autocomplete="given-name" required>

    <label for="last_name">Last Name</label>
    <input id="last_name" name="last_name" autocomplete="family-name" required>

    <label for="email">Email</label>
    <input id="email" name="email" type="email" autocomplete="email" required>

    <label for="phone">Phone</label>
    <input id="phone" name="phone" autocomplete="tel" required>

    <label for="address_line1">Street Address</label>
    <input id="address_line1" name="address_line1" autocomplete="address-line1" required>

    <label for="city">City</label>
    <input id="city" name="city" required>

    <label for="state">State</label>
    <select id="state" name="state" required>
      <option value="">Select</option>
      <option>Texas</option>
      <option>Louisiana</option>
      <option>Florida</option>
    </select>

    <label for="zip">ZIP</label>
    <input id="zip" name="zip" autocomplete="postal-code" required>

    <label for="date_available">Date Available</label>
    <input id="date_available" name="date_available">

    <label for="desired_pay">Desired Pay</label>
    <input id="desired_pay" name="desired_pay">

    <label for="pitch">Cover Letter / Comments</label>
    <textarea id="pitch" name="pitch" rows="5"></textarea>

    <label for="resume">Resume</label>
    <input id="resume" name="resume" type="file">

    <fieldset>
      <legend>Are you legally authorized to work in the United States?</legend>
      <label class="inline"><input type="radio" name="authorized" value="Yes"> Yes</label>
      <label class="inline"><input type="radio" name="authorized" value="No"> No</label>
    </fieldset>

    <fieldset>
      <legend>Will you require sponsorship now or in the future?</legend>
      <label class="inline"><input type="radio" name="sponsorship" value="Yes"> Yes</label>
      <label class="inline"><input type="radio" name="sponsorship" value="No"> No</label>
    </fieldset>

    <div aria-hidden="true">
      <label for="website">Website</label>
      <input id="website" name="website" tabindex="-1">
    </div>

    <div class="actions">
      <button type="submit">{submit_text}</button>
    </div>
  </form>
</div>
"""
    return page_template(f"{company['company']} application", body)


def confirmation_page(company: dict[str, str]) -> bytes:
    body = f"""
<h1>Application confirmation</h1>
<div class="card">
  <p>Thank you for applying to {company['company']}.</p>
  <p>Your application has been submitted successfully.</p>
  <p>We have received your application and our maritime hiring team will review it shortly.</p>
  <p>Application number: MOCK-{company['slug'].upper()}</p>
</div>
"""
    return page_template(f"{company['company']} confirmation", body)


class MockHandler(BaseHTTPRequestHandler):
    server_version = "MockMaritimeATS/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        print("%s - - [%s] %s" % (self.client_address[0], self.log_date_time_string(), fmt % args), flush=True)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self._send_text("ok")
            return
        if parsed.path == "/targets.json":
            self._send_json(self.server.targets)  # type: ignore[attr-defined]
            return

        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 3 and parts[0] == "company":
            slug = parts[1]
            company = company_by_slug(slug)
            if not company:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            page = parts[2]
            if page == "jobs":
                self._send_html(jobs_page(company))
                return
            if page == "job":
                self._send_html(job_page(company))
                return
            if page == "apply":
                step = 2 if parse_qs(parsed.query).get("step") == ["2"] else 1
                self._send_html(application_form(company, step))
                return
            if page == "application-confirmation":
                self._send_html(confirmation_page(company))
                return

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 3 and parts[0] == "company":
            slug = parts[1]
            company = company_by_slug(slug)
            if not company:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            content_length = int(self.headers.get("Content-Length", "0") or "0")
            if content_length:
                self.rfile.read(content_length)

            page = parts[2]
            if page == "apply":
                step = 2 if parse_qs(parsed.query).get("step") == ["2"] else 1
                if company["mode"] == "multi" and step == 1:
                    self._redirect(f"/company/{slug}/apply?step=2")
                    return
                self._redirect(f"/company/{slug}/application-confirmation")
                return
            if page == "submit":
                self._redirect(f"/company/{slug}/application-confirmation")
                return

        self.send_error(HTTPStatus.NOT_FOUND)

    def _send_text(self, text: str, status: int = 200) -> None:
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, payload: object, status: int = 200) -> None:
        data = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, data: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()


def main() -> None:
    args = parse_args()
    targets = build_targets(args.host, args.port)
    if args.targets_out:
        path = Path(args.targets_out)
        path.write_text(json.dumps(targets, indent=2), encoding="utf-8")
    server = ThreadingHTTPServer((args.host, args.port), MockHandler)
    server.targets = targets  # type: ignore[attr-defined]
    print(f"Mock maritime ATS listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
