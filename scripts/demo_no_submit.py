#!/usr/bin/env python3
"""Record a no-submit agent demo and generate an HTML report."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path
from shutil import move
from typing import Any

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import swarm


DEMO_BANNER_ID = "swarm-demo-banner"
DEMO_STEPS_ID = "swarm-demo-steps"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record a no-submit automation demo.")
    parser.add_argument("--company", default="Curtin Maritime")
    parser.add_argument("--output-dir", default="proof/demo")
    parser.add_argument("--slow-mo", type=int, default=300)
    parser.add_argument("--headful", action="store_true")
    parser.add_argument("--viewport-width", type=int, default=1440)
    parser.add_argument("--viewport-height", type=int, default=1080)
    return parser.parse_args()


async def ensure_overlay(page: Any) -> None:
    await page.evaluate(
        f"""() => {{
            if (!document.getElementById("{DEMO_BANNER_ID}")) {{
                const banner = document.createElement('div');
                banner.id = "{DEMO_BANNER_ID}";
                banner.style.position = 'fixed';
                banner.style.top = '12px';
                banner.style.left = '12px';
                banner.style.right = '12px';
                banner.style.zIndex = '2147483647';
                banner.style.padding = '12px 16px';
                banner.style.background = 'rgba(180, 0, 0, 0.92)';
                banner.style.color = '#fff';
                banner.style.fontSize = '20px';
                banner.style.fontWeight = '700';
                banner.style.textAlign = 'center';
                banner.style.borderRadius = '8px';
                banner.style.boxShadow = '0 6px 24px rgba(0,0,0,.25)';
                document.body.appendChild(banner);
            }}
            if (!document.getElementById("{DEMO_STEPS_ID}")) {{
                const panel = document.createElement('div');
                panel.id = "{DEMO_STEPS_ID}";
                panel.style.position = 'fixed';
                panel.style.right = '12px';
                panel.style.bottom = '12px';
                panel.style.zIndex = '2147483647';
                panel.style.maxWidth = '460px';
                panel.style.padding = '12px 14px';
                panel.style.background = 'rgba(16, 24, 40, 0.88)';
                panel.style.color = '#fff';
                panel.style.fontSize = '16px';
                panel.style.lineHeight = '1.4';
                panel.style.borderRadius = '8px';
                panel.style.boxShadow = '0 6px 24px rgba(0,0,0,.25)';
                document.body.appendChild(panel);
            }}
        }}"""
    )


async def set_overlay(page: Any, title: str, steps: list[str]) -> None:
    await ensure_overlay(page)
    await page.evaluate(
        f"""([title, steps]) => {{
            const banner = document.getElementById("{DEMO_BANNER_ID}");
            const panel = document.getElementById("{DEMO_STEPS_ID}");
            if (banner) banner.textContent = title;
            if (panel) {{
                panel.innerHTML = '<strong>Agent actions</strong><br>' + steps.map(
                    (step, idx) => `${{idx + 1}}. ${{step}}`
                ).join('<br>');
            }}
        }}""",
        [title, steps],
    )


async def native_bamboo_dropdowns(page: Any) -> list[dict[str, str]]:
    dropdowns = [
        ("State", ["Texas"]),
        ("Gender", ["Decline to Answer", "Decline to answer", "Decline"]),
        ("Ethnicity", ["Black or African American", "Black"]),
        ("Disability", ["Decline to Answer", "Decline to answer", "No", "None"]),
    ]
    picked: list[dict[str, str]] = []
    for label, options in dropdowns:
        try:
            btn = page.locator(f'button:has-text("{label}"), button[aria-label*="{label}"]').first
            if await btn.count() == 0:
                continue
            await btn.scroll_into_view_if_needed(timeout=2000)
            await btn.click(timeout=3000)
            await page.wait_for_timeout(500)
            for option in options:
                choice = page.locator(
                    '.fab-MenuOption, .fab-MenuOption__content, [role="option"], [role="menuitem"]',
                    has_text=option,
                ).first
                if await choice.count() > 0:
                    await choice.click(timeout=3000)
                    await page.wait_for_timeout(300)
                    picked.append({"field": label, "value": option})
                    break
            else:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(200)
        except Exception:
            continue
    return picked


async def choose_curtin_job(page: Any) -> str:
    job_links = page.locator('a[href*="/careers/"]')
    count = await job_links.count()
    chosen_job = "first visible job"
    best_idx = 0
    best_score = -999
    keywords = ["deck", "marine", "vessel", "crew", "entry", "cook"]
    for idx in range(min(count, 12)):
        text = (await job_links.nth(idx).inner_text(timeout=1000) or "").strip().lower()
        score = sum(3 for kw in keywords if kw in text)
        if score > best_score:
            best_score = score
            best_idx = idx
            chosen_job = text or chosen_job
    await job_links.nth(best_idx).click(timeout=5000)
    return chosen_job


def render_report(metadata: dict[str, Any], output_dir: Path, mp4_name: str | None) -> Path:
    report_path = output_dir / "index.html"
    video_block = (
        f'<video controls playsinline style="width:100%;max-width:1100px;border-radius:12px;" src="{mp4_name}"></video>'
        if mp4_name
        else "<p><strong>MP4 conversion unavailable.</strong> Open the WebM file directly from this folder.</p>"
    )
    steps_html = "".join(f"<li>{step}</li>" for step in metadata.get("steps", []))
    dropdown_rows = "".join(
        f"<li><strong>{item['field']}:</strong> {item['value']}</li>" for item in metadata.get("dropdowns", [])
    )
    report_path.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>No-submit agent demo</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 0; background: #0b1220; color: #f8fafc; }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 24px; }}
    .card {{ background: #111827; border: 1px solid #334155; border-radius: 14px; padding: 18px 20px; margin-bottom: 18px; }}
    a {{ color: #93c5fd; }}
    code, pre {{ background: #020617; border-radius: 10px; padding: 2px 6px; }}
    pre {{ padding: 14px; overflow-x: auto; }}
    img {{ max-width: 100%; border-radius: 12px; border: 1px solid #334155; }}
    ul {{ margin-top: 8px; }}
  </style>
</head>
<body>
<main>
  <div class="card">
    <h1>No-submit agent demo</h1>
    <p><strong>Target:</strong> {metadata['target']}</p>
    <p><strong>Chosen job:</strong> {metadata['chosen_job']}</p>
    <p><strong>Final URL:</strong> <a href="{metadata['url']}">{metadata['url']}</a></p>
    <p><strong>Submitted:</strong> {str(metadata['submitted']).lower()}</p>
    <p><strong>Safety banner:</strong> {metadata['banner']}</p>
  </div>

  <div class="card">
    <h2>Video</h2>
    {video_block}
  </div>

  <div class="card">
    <h2>Final frame</h2>
    <img alt="Final no-submit frame" src="{metadata['screenshot_name']}">
  </div>

  <div class="card">
    <h2>Agent actions shown in the demo</h2>
    <ol>{steps_html}</ol>
  </div>

  <div class="card">
    <h2>Run metadata</h2>
    <ul>
      <li><strong>filled_count:</strong> {metadata['filled_count']}</li>
      <li><strong>eeo_actions:</strong> {metadata['eeo_actions']}</li>
      <li><strong>resume_uploads:</strong> {metadata['resume_uploads']}</li>
    </ul>
    <h3>Dropdowns exercised</h3>
    <ul>{dropdown_rows or '<li>None</li>'}</ul>
    <pre>{json.dumps(metadata, indent=2)}</pre>
  </div>
</main>
</body>
</html>
""",
        encoding="utf-8",
    )
    return report_path


def convert_to_mp4(source: Path, output: Path) -> bool:
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(source),
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(output),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


async def record_demo(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    webm_path = output_dir / "curtin-no-submit-demo.webm"
    mp4_path = output_dir / "curtin-no-submit-demo.mp4"
    screenshot_path = output_dir / "curtin-no-submit-final.png"
    metadata_path = output_dir / "curtin-no-submit-demo.json"

    for path in [webm_path, mp4_path, screenshot_path, metadata_path, output_dir / "index.html"]:
        if path.exists():
            path.unlink()

    target = next(t for t in swarm.TARGETS if t["company"] == args.company)
    profile = swarm.build_target_profile(swarm.load_profile(), target)

    steps: list[str] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=not args.headful,
            slow_mo=max(0, int(args.slow_mo)),
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        context = await browser.new_context(
            ignore_https_errors=True,
            viewport={"width": args.viewport_width, "height": args.viewport_height},
            record_video_dir=str(output_dir),
            record_video_size={"width": 1280, "height": 720},
        )
        page = await context.new_page()
        video = page.video

        await page.add_init_script(swarm.INJECT_HELPER_JS)

        steps.append("Agent opens the job board")
        await page.goto(target["url"], wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(1800)
        await swarm.reinject(page)
        await set_overlay(page, "Agent demo: opening target site", steps)
        await swarm.click_hints(page, swarm.COOKIE_HINTS)
        await page.wait_for_timeout(1000)

        steps.append("Agent chooses a relevant job listing")
        chosen_job = await choose_curtin_job(page)
        await page.wait_for_timeout(2000)
        await set_overlay(page, "Agent demo: selecting a job listing", steps)

        steps.append("Agent opens the application form")
        apply_btn = page.locator(
            '.BambooHR-ATS-board__apply-btn, a[href*="applicationModal"], a:has-text("Apply"), button:has-text("Apply")'
        ).first
        await apply_btn.scroll_into_view_if_needed(timeout=3000)
        await page.wait_for_timeout(600)
        await apply_btn.click(timeout=5000)
        await page.wait_for_timeout(3500)
        await set_overlay(page, "Agent demo: opening the application form", steps)

        steps.append("Agent fills the profile fields")
        filled, eeo = await swarm.apply_profile(page, profile)
        await page.wait_for_timeout(1000)
        await set_overlay(page, "Agent demo: filling profile data", steps)

        steps.append("Agent selects EEO and state dropdown answers")
        dropdowns = await native_bamboo_dropdowns(page)
        await page.wait_for_timeout(800)
        await set_overlay(page, "Agent demo: selecting dropdown answers", steps)

        steps.append("Agent uploads the resume when possible")
        uploaded = await swarm.upload_resume(page, Path("/workspace") / str(profile.get("resume_path", "./resume.pdf")))
        await page.wait_for_timeout(1200)
        await set_overlay(page, "Agent demo: attempting resume upload", steps)

        steps.append("Agent performs a second fill pass to stabilize the form")
        filled2, eeo2 = await swarm.apply_profile(page, profile)
        filled += filled2
        eeo += eeo2
        await page.wait_for_timeout(900)
        await set_overlay(page, "Agent demo: stabilizing filled values", steps)

        steps.append("Agent stops before submit and leaves the final button untouched")
        submit_btn = page.locator(
            'button:has-text("Submit Application"), input[type="submit"][value*="Submit"], button[type="submit"]'
        ).first
        if await submit_btn.count() > 0:
            await submit_btn.scroll_into_view_if_needed(timeout=3000)
        await set_overlay(page, "Demo paused before submit - no application was submitted", steps)
        await page.wait_for_timeout(2500)
        await page.screenshot(path=str(screenshot_path), full_page=True)

        metadata = {
            "target": target["company"],
            "url": page.url,
            "chosen_job": chosen_job,
            "filled_count": filled,
            "eeo_actions": eeo,
            "resume_uploads": uploaded,
            "dropdowns": dropdowns,
            "submitted": False,
            "banner": "Demo paused before submit - no application was submitted",
            "steps": steps,
            "screenshot_name": screenshot_path.name,
            "webm_name": webm_path.name,
        }
        metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

        await context.close()
        source_video = Path(await video.path())
        move(str(source_video), str(webm_path))
        await browser.close()

    mp4_name: str | None = None
    if convert_to_mp4(webm_path, mp4_path):
        mp4_name = mp4_path.name

    metadata["mp4_name"] = mp4_name
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    report_path = render_report(metadata, Path(args.output_dir), mp4_name)
    metadata["report_name"] = report_path.name
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return metadata


def main() -> None:
    args = parse_args()
    metadata = asyncio.run(record_demo(args))
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
