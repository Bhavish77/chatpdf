"""One-off script: converts seed_raw.txt (the plain-text Project Gutenberg
edition of "The Elements of Style") into a readable, paginated PDF. Not part
of the app; run once to produce seed/elements_of_style.pdf. Needs reportlab,
which is NOT a runtime dependency - install it just to run this script.
"""

import html
import sys
from pathlib import Path

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer


def main() -> None:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "seed_raw.txt")
    dst = Path(sys.argv[2] if len(sys.argv) > 2 else "seed/elements_of_style.pdf")
    dst.parent.mkdir(parents=True, exist_ok=True)

    text = src.read_text(encoding="utf-8")
    paragraphs = [p.strip() for p in text.split("\n\n")]

    body_style = ParagraphStyle("body", fontName="Times-Roman", fontSize=11, leading=15, spaceAfter=10)
    heading_style = ParagraphStyle(
        "heading", fontName="Times-Bold", fontSize=13, leading=17, spaceBefore=14, spaceAfter=8
    )

    doc = SimpleDocTemplate(
        str(dst),
        pagesize=LETTER,
        leftMargin=1 * inch,
        rightMargin=1 * inch,
        topMargin=1 * inch,
        bottomMargin=1 * inch,
        title="The Elements of Style",
        author="William Strunk Jr.",
    )

    flowables = []
    for para in paragraphs:
        if not para:
            continue
        escaped = html.escape(para).replace("\n", "<br/>")
        is_heading = len(para) < 60 and para.isupper()
        style = heading_style if is_heading else body_style
        flowables.append(Paragraph(escaped, style))
        flowables.append(Spacer(1, 0))

    doc.build(flowables)
    print(f"Wrote {dst} ({dst.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
