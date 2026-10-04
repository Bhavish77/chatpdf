"""DOCX test fixtures, built with the python-docx dependency we already ship."""

import io

from docx import Document as DocxDocument


def make_docx_bytes(paragraphs: list[str]) -> bytes:
    doc = DocxDocument()
    for text in paragraphs:
        doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
