"""Per-page recursive character chunking: splitting each page on its own keeps
page numbers attached to every chunk, which is what makes citations possible.
"""

from dataclasses import dataclass

from langchain_text_splitters import RecursiveCharacterTextSplitter

CHUNK_SIZE = 1000
CHUNK_OVERLAP = 150


@dataclass(frozen=True, slots=True)
class Chunk:
    index: int
    page: int | None
    content: str


def chunk_pages(pages: list[str], *, paged: bool, max_chunks: int) -> list[Chunk]:
    splitter = RecursiveCharacterTextSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)
    chunks: list[Chunk] = []
    for page_num, page_text in enumerate(pages, start=1):
        if not page_text or not page_text.strip():
            continue
        for piece in splitter.split_text(page_text):
            if not piece.strip():
                continue
            chunks.append(Chunk(index=len(chunks), page=page_num if paged else None, content=piece))
            if len(chunks) >= max_chunks:
                return chunks
    return chunks
