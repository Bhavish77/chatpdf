"""Extract per-page text from PDF, DOCX, TXT and MD bytes.

DOCX, TXT and MD have no native page boundaries, so they come back as a
single-element list (an unpaged "page") and chunks.py records page=None for
them, matching the `chunks.page` column being nullable for unpaged formats.
"""

import io

from docx import Document as DocxDocument
from pypdf import PdfReader

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TXT_MIME = "text/plain"
MD_MIME = "text/markdown"

PAGED_MIMES = {PDF_MIME}


class PermanentParseError(Exception):
    """Not retryable: corrupt file, no extractable text, or over the page limit."""


def parse_pdf(data: bytes, max_pages: int) -> list[str]:
    try:
        reader = PdfReader(io.BytesIO(data))
        page_count = len(reader.pages)
    except Exception as exc:
        raise PermanentParseError(f"Could not read PDF: {exc}") from exc

    if page_count > max_pages:
        raise PermanentParseError(f"PDF has {page_count} pages, over the {max_pages} page limit")

    pages: list[str] = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 - a single bad page shouldn't fail the whole document
            pages.append("")

    if not any(p.strip() for p in pages):
        raise PermanentParseError("No extractable text (scanned PDF?). OCR is not supported in this demo.")
    return pages


def parse_docx(data: bytes) -> list[str]:
    try:
        doc = DocxDocument(io.BytesIO(data))
    except Exception as exc:
        raise PermanentParseError(f"Could not read DOCX: {exc}") from exc

    text = "\n".join(p.text for p in doc.paragraphs)
    if not text.strip():
        raise PermanentParseError("No extractable text in this document.")
    return [text]


def parse_text(data: bytes) -> list[str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PermanentParseError(f"Not valid UTF-8 text: {exc}") from exc
    if not text.strip():
        raise PermanentParseError("File is empty.")
    return [text]


def parse(mime: str, data: bytes, max_pages: int) -> list[str]:
    if mime == PDF_MIME:
        return parse_pdf(data, max_pages)
    if mime == DOCX_MIME:
        return parse_docx(data)
    if mime in (TXT_MIME, MD_MIME):
        return parse_text(data)
    raise PermanentParseError(f"Unsupported file type: {mime}")
