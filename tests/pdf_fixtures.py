"""A minimal, hand-built PDF writer for test fixtures: real extractable text
via the standard (non-embedded) Helvetica font, without pulling in a
PDF-writing dependency just for tests.
"""


def _esc(s: str) -> str:
    return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def make_pdf_bytes(pages: list[str]) -> bytes:
    font_obj_num = 3
    body_parts: list[tuple[int, bytes]] = []

    font_obj = f"{font_obj_num} 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n"
    body_parts.append((font_obj_num, font_obj.encode("latin-1")))

    obj_num = font_obj_num + 1
    kids_nums: list[int] = []
    for text in pages:
        content = f"BT /F1 14 Tf 72 712 Td ({_esc(text[:500])}) Tj ET"
        content_bytes = content.encode("latin-1", errors="replace")
        stream_obj_num = obj_num
        stream_obj = (
            f"{stream_obj_num} 0 obj\n<< /Length {len(content_bytes)} >>\nstream\n".encode("latin-1")
            + content_bytes
            + b"\nendstream\nendobj\n"
        )
        obj_num += 1

        page_obj_num = obj_num
        page_obj = (
            f"{page_obj_num} 0 obj\n<< /Type /Page /Parent 2 0 R "
            f"/Resources << /Font << /F1 {font_obj_num} 0 R >> >> "
            f"/MediaBox [0 0 612 792] /Contents {stream_obj_num} 0 R >>\nendobj\n"
        ).encode("latin-1")
        obj_num += 1

        kids_nums.append(page_obj_num)
        body_parts.append((stream_obj_num, stream_obj))
        body_parts.append((page_obj_num, page_obj))

    kids_str = " ".join(f"{n} 0 R" for n in kids_nums)
    pages_obj = f"2 0 obj\n<< /Type /Pages /Kids [{kids_str}] /Count {len(pages)} >>\nendobj\n".encode(
        "latin-1"
    )
    catalog_obj = b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"

    all_objs = sorted([(1, catalog_obj), (2, pages_obj), *body_parts], key=lambda x: x[0])

    out = b"%PDF-1.4\n"
    offsets: dict[int, int] = {}
    for num, data in all_objs:
        offsets[num] = len(out)
        out += data

    xref_start = len(out)
    max_num = max(offsets)
    out += f"xref\n0 {max_num + 1}\n".encode("latin-1")
    out += b"0000000000 65535 f \n"
    for i in range(1, max_num + 1):
        out += f"{offsets.get(i, 0):010d} 00000 n \n".encode("latin-1")
    out += f"trailer\n<< /Size {max_num + 1} /Root 1 0 R >>\nstartxref\n{xref_start}\n%%EOF".encode(
        "latin-1"
    )
    return out
