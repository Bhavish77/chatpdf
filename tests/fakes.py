"""A fake Gemini client for tests: deterministic embeddings, no network calls,
and configurable failure injection. Never call the real API in tests (BUILD_SPEC
section 4, rule 6)."""

import hashlib
from collections.abc import AsyncIterator

from app.llm import PermanentError, QuotaExhausted


def deterministic_vector(text: str, dim: int = 768) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [digest[i % len(digest)] / 255.0 for i in range(dim)]


class FakeLLMClient:
    """fail_times counts calls to embed_documents: the first `fail_times` calls
    raise (QuotaExhausted, or PermanentError if permanent=True); later calls
    succeed. This models a client whose own internal retry already gave up
    (if permanent) or that keeps getting 429s across separate job attempts."""

    def __init__(self, *, fail_times: int = 0, permanent: bool = False) -> None:
        self.fail_times = fail_times
        self.permanent = permanent
        self.embed_calls = 0
        self.generate_calls = 0

    async def embed_documents(self, texts: list[str], *, title: str | None = None) -> list[list[float]]:
        self.embed_calls += 1
        if self.embed_calls <= self.fail_times:
            if self.permanent:
                raise PermanentError("simulated permanent embedding failure")
            raise QuotaExhausted("simulated 429")
        return [deterministic_vector(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return deterministic_vector(text)

    async def generate_json(self, *, model: str, system: str, prompt: str, schema: dict) -> dict:
        self.generate_calls += 1
        return {}

    async def generate_stream(self, *, model: str, system: str, prompt: str) -> AsyncIterator[str]:
        self.generate_calls += 1
        for word in ["This ", "is ", "a ", "fake ", "answer."]:
            yield word
