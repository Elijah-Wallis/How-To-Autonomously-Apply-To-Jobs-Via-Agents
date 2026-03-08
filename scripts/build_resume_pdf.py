#!/usr/bin/env python3
"""Build a simple ATS-friendly PDF resume from resume.md."""

from __future__ import annotations

import argparse
import textwrap
from dataclasses import dataclass
from pathlib import Path


PAGE_WIDTH = 612
PAGE_HEIGHT = 792
LEFT_MARGIN = 54
TOP_MARGIN = 54
BOTTOM_MARGIN = 48
CONTENT_WIDTH = 504


@dataclass
class StyledLine:
    text: str
    font: str
    font_size: float
    indent: int = 0
    gap_after: float = 0.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build resume.pdf from resume.md")
    parser.add_argument("--source", default="resume.md")
    parser.add_argument("--output", default="resume.pdf")
    return parser.parse_args()


def markdown_to_lines(text: str) -> list[StyledLine]:
    lines: list[StyledLine] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            lines.append(StyledLine("", "F1", 10.5, gap_after=4))
            continue
        if stripped.startswith("# "):
            lines.append(StyledLine(stripped[2:].strip(), "F2", 18, gap_after=4))
            continue
        if stripped.startswith("## "):
            lines.append(StyledLine(stripped[3:].strip(), "F2", 12.5, gap_after=3))
            continue
        if stripped.startswith("### "):
            lines.append(StyledLine(stripped[4:].strip(), "F2", 11.5, gap_after=2))
            continue
        if stripped.startswith("- "):
            bullet = stripped[2:].strip()
            wrapped = textwrap.wrap(
                bullet,
                width=88,
                initial_indent="- ",
                subsequent_indent="  ",
                break_long_words=False,
                break_on_hyphens=False,
            )
            for item in wrapped:
                lines.append(StyledLine(item, "F1", 10.5, indent=10))
            continue

        wrapped = textwrap.wrap(
            stripped,
            width=94,
            break_long_words=False,
            break_on_hyphens=False,
        )
        for item in wrapped:
            lines.append(StyledLine(item, "F1", 10.5))
    return lines


def escape_pdf_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build_page_stream(page_lines: list[tuple[float, StyledLine]]) -> str:
    chunks = ["BT"]
    for y, line in page_lines:
        if not line.text:
            continue
        x = LEFT_MARGIN + line.indent
        chunks.append(f"/{line.font} {line.font_size:.2f} Tf")
        chunks.append(f"1 0 0 1 {x:.2f} {y:.2f} Tm")
        chunks.append(f"({escape_pdf_text(line.text)}) Tj")
    chunks.append("ET")
    return "\n".join(chunks) + "\n"


def paginate(lines: list[StyledLine]) -> list[str]:
    pages: list[list[tuple[float, StyledLine]]] = []
    current: list[tuple[float, StyledLine]] = []
    y = PAGE_HEIGHT - TOP_MARGIN

    for line in lines:
        line_height = max(12.0, line.font_size + 3.0)
        if y - line_height < BOTTOM_MARGIN:
            pages.append(current)
            current = []
            y = PAGE_HEIGHT - TOP_MARGIN
        current.append((y, line))
        y -= line_height + line.gap_after

    if current:
        pages.append(current)

    return [build_page_stream(page) for page in pages]


def build_pdf(page_streams: list[str]) -> bytes:
    objects: list[bytes] = []

    def add_object(data: str | bytes) -> int:
        payload = data.encode("latin-1") if isinstance(data, str) else data
        objects.append(payload)
        return len(objects)

    font_regular_id = add_object("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    font_bold_id = add_object("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")

    content_ids: list[int] = []
    page_ids: list[int] = []

    pages_id_placeholder = len(objects) + 1
    add_object("<< /Type /Pages /Count 0 /Kids [] >>")

    for stream in page_streams:
        encoded = stream.encode("latin-1")
        content_id = add_object(
            b"<< /Length " + str(len(encoded)).encode("ascii") + b" >>\nstream\n" + encoded + b"endstream"
        )
        content_ids.append(content_id)
        page_obj = (
            f"<< /Type /Page /Parent {pages_id_placeholder} 0 R "
            f"/MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
            f"/Resources << /Font << /F1 {font_regular_id} 0 R /F2 {font_bold_id} 0 R >> >> "
            f"/Contents {content_id} 0 R >>"
        )
        page_ids.append(add_object(page_obj))

    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects[pages_id_placeholder - 1] = (
        f"<< /Type /Pages /Count {len(page_ids)} /Kids [{kids}] >>".encode("latin-1")
    )

    catalog_id = add_object(f"<< /Type /Catalog /Pages {pages_id_placeholder} 0 R >>")

    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{index} 0 obj\n".encode("ascii"))
        pdf.extend(obj)
        pdf.extend(b"\nendobj\n")

    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(pdf)


def main() -> None:
    args = parse_args()
    source = Path(args.source)
    output = Path(args.output)
    text = source.read_text(encoding="utf-8")
    lines = markdown_to_lines(text)
    page_streams = paginate(lines)
    output.write_bytes(build_pdf(page_streams))
    print(output.resolve())


if __name__ == "__main__":
    main()
