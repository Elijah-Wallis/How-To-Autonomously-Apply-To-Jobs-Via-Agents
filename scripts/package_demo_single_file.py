#!/usr/bin/env python3
"""Package the recorded demo into one self-contained HTML file."""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Package demo artifacts into one HTML file.")
    parser.add_argument("--video", required=True, help="Path to MP4 video")
    parser.add_argument("--image", required=True, help="Path to PNG screenshot")
    parser.add_argument("--metadata", required=True, help="Path to demo metadata JSON")
    parser.add_argument("--output", default="demo/index.html", help="Output HTML path")
    parser.add_argument("--compile-status", default="PASS")
    parser.add_argument("--unit-test-status", default="PASS")
    parser.add_argument("--acceptance-status", default="NOT GREEN")
    parser.add_argument(
        "--acceptance-notes",
        default=(
            "The single no-submit demo passed, but the full end-to-end submit acceptance suite "
            "across all employers is not yet fully green."
        ),
    )
    return parser.parse_args()


def data_uri(path: Path, mime_type: str) -> str:
    return f"data:{mime_type};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def status_badge(label: str, value: str) -> str:
    safe = value.strip().upper()
    cls = "pass" if safe in {"PASS", "GREEN"} else "warn"
    return f'<div class="status {cls}"><strong>{label}:</strong> {value}</div>'


def main() -> None:
    args = parse_args()
    video_path = Path(args.video).resolve()
    image_path = Path(args.image).resolve()
    metadata_path = Path(args.metadata).resolve()
    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    video_uri = data_uri(video_path, "video/mp4")
    image_uri = data_uri(image_path, "image/png")

    steps_html = "".join(f"<li>{step}</li>" for step in metadata.get("steps", []))
    dropdown_html = "".join(
        f"<li><strong>{item['field']}:</strong> {item['value']}</li>"
        for item in metadata.get("dropdowns", [])
    )

    compile_badge = status_badge("py_compile", args.compile_status)
    unit_badge = status_badge("unit tests", args.unit_test_status)
    acceptance_badge = status_badge("full acceptance gates", args.acceptance_status)

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Agent demo package</title>
  <style>
    body {{ margin: 0; background: #020617; color: #e2e8f0; font-family: Arial, sans-serif; }}
    main {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}
    .card {{ background: #0f172a; border: 1px solid #334155; border-radius: 14px; padding: 18px 20px; margin-bottom: 18px; }}
    .status {{ display: inline-block; margin: 0 10px 10px 0; padding: 10px 12px; border-radius: 999px; }}
    .status.pass {{ background: #14532d; color: #dcfce7; }}
    .status.warn {{ background: #7c2d12; color: #ffedd5; }}
    video, img {{ width: 100%; border-radius: 12px; border: 1px solid #475569; }}
    pre {{ background: #020617; padding: 14px; border-radius: 12px; overflow-x: auto; }}
    a {{ color: #93c5fd; }}
    ul, ol {{ line-height: 1.55; }}
  </style>
</head>
<body>
<main>
  <div class="card">
    <h1>Single-file agent demo package</h1>
    <p><strong>Target:</strong> {metadata['target']}</p>
    <p><strong>Chosen job:</strong> {metadata['chosen_job']}</p>
    <p><strong>Final URL:</strong> <a href="{metadata['url']}">{metadata['url']}</a></p>
    <p><strong>Submitted:</strong> {str(metadata['submitted']).lower()}</p>
    <p><strong>Safety banner:</strong> {metadata['banner']}</p>
  </div>

  <div class="card">
    <h2>Status summary</h2>
    {compile_badge}
    {unit_badge}
    {acceptance_badge}
    <p>{args.acceptance_notes}</p>
  </div>

  <div class="card">
    <h2>Recorded demo video</h2>
    <video controls playsinline src="{video_uri}"></video>
  </div>

  <div class="card">
    <h2>Final frame before submit</h2>
    <img alt="Final no-submit frame" src="{image_uri}">
  </div>

  <div class="card">
    <h2>Agent actions shown</h2>
    <ol>{steps_html}</ol>
  </div>

  <div class="card">
    <h2>Metadata</h2>
    <ul>
      <li><strong>filled_count:</strong> {metadata['filled_count']}</li>
      <li><strong>eeo_actions:</strong> {metadata['eeo_actions']}</li>
      <li><strong>resume_uploads:</strong> {metadata['resume_uploads']}</li>
    </ul>
    <h3>Dropdowns exercised</h3>
    <ul>{dropdown_html or '<li>None</li>'}</ul>
    <pre>{json.dumps(metadata, indent=2)}</pre>
  </div>
</main>
</body>
</html>
"""
    output_path.write_text(html, encoding="utf-8")
    print(output_path)


if __name__ == "__main__":
    main()
