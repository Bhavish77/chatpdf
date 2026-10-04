"""Gemini client: the one seam between the app and the LLM vendor.

# PROD: swap for a provider router with fallbacks across vendors, a
# response/embedding cache keyed by content hash, and per-tenant budgets.
"""

import asyncio
import json
import logging
import random
import time
from collections.abc import AsyncIterator
from typing import Protocol

from google import genai
from google.genai import types

from app.config import Settings

logger = logging.getLogger(__name__)


class TransientLLMError(Exception):
    """Retryable: 429, 5xx, timeouts, transport errors."""


class QuotaExhausted(TransientLLMError):
    """429 that survived every retry."""


class PermanentError(Exception):
    """Not retryable: bad request, rejected model name, etc."""


class LLMClient(Protocol):
    async def embed_documents(
        self, texts: list[str], *, title: str | None = None
    ) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...

    async def generate_json(self, *, model: str, system: str, prompt: str, schema: dict) -> dict: ...

    def generate_stream(self, *, model: str, system: str, prompt: str) -> AsyncIterator[str]: ...


_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 4
_BASE_DELAY_S = 1.0


def _classify(exc: Exception) -> Exception:
    """Map any exception raised by the google-genai SDK call to one of our typed errors."""
    if isinstance(exc, (TransientLLMError, PermanentError)):
        return exc
    code = getattr(exc, "code", None)
    if code == 429:
        return QuotaExhausted(str(exc))
    if code in _RETRYABLE_STATUS:
        return TransientLLMError(str(exc))
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return TransientLLMError(str(exc))
    return PermanentError(str(exc))


async def _with_retry(fn, *, max_attempts: int = _MAX_ATTEMPTS, base_delay: float = _BASE_DELAY_S):
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001 - reclassified immediately below
            classified = _classify(exc)
            if isinstance(classified, PermanentError):
                raise classified from exc
            last_exc = classified
            if attempt == max_attempts:
                break
            delay = base_delay * (2 ** (attempt - 1)) * (1 + random.random() * 0.25)
            logger.warning("llm call failed (attempt %s/%s): %s", attempt, max_attempts, classified)
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc


def _query_text(text: str) -> str:
    # Asymmetric-retrieval prefix for gemini-embedding-2 (ai.google.dev/gemini-api/docs/embeddings,
    # checked 4 Oct 2026). The model has no task_type parameter; the prefix IS the mechanism.
    return f"task: search result | query: {text}"


def _document_text(text: str, title: str | None) -> str:
    return f"title: {title or 'none'} | text: {text}"


class GeminiClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client_instance: genai.Client | None = None
        self._concurrency = asyncio.Semaphore(settings.LLM_MAX_CONCURRENCY)
        self._embed_min_interval = settings.EMBED_MIN_INTERVAL_S
        self._embed_lock = asyncio.Lock()
        self._last_embed_call = 0.0

    @property
    def _client(self) -> genai.Client:
        # Constructed lazily, not in __init__: genai.Client() validates the
        # API key eagerly and raises if it's empty. GeminiClient is built
        # once at process startup (API and worker both construct one
        # regardless of whether a request ever needs it), so an empty key
        # must not crash the whole process - only the first actual call
        # that needs Gemini should fail, with a clear error.
        if self._client_instance is None:
            self._client_instance = genai.Client(api_key=self._settings.GEMINI_API_KEY)
        return self._client_instance

    async def _throttle_embed(self) -> None:
        async with self._embed_lock:
            now = time.monotonic()
            wait = self._last_embed_call + self._embed_min_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_embed_call = time.monotonic()

    async def embed_documents(
        self, texts: list[str], *, title: str | None = None
    ) -> list[list[float]]:
        # DEVIATION (checked live against the real API, 4 Oct 2026): passing a
        # plain list[str] as `contents` does NOT batch independent documents -
        # the API treats the strings as multiple *parts of one* Content and
        # returns a single embedding for all of them combined. Wrapping each
        # text in its own types.Content is what actually gets one embedding
        # per input back.
        contents = [types.Content(parts=[types.Part(text=_document_text(t, title))]) for t in texts]

        async def _call() -> list[list[float]]:
            await self._throttle_embed()
            async with self._concurrency:
                resp = await self._client.aio.models.embed_content(
                    model=self._settings.EMBED_MODEL,
                    contents=contents,
                    config=types.EmbedContentConfig(output_dimensionality=self._settings.EMBED_DIM),
                )
            return [e.values for e in resp.embeddings]

        return await _with_retry(_call)

    async def embed_query(self, text: str) -> list[float]:
        contents = [types.Content(parts=[types.Part(text=_query_text(text))])]

        async def _call() -> list[list[float]]:
            await self._throttle_embed()
            async with self._concurrency:
                resp = await self._client.aio.models.embed_content(
                    model=self._settings.EMBED_MODEL,
                    contents=contents,
                    config=types.EmbedContentConfig(output_dimensionality=self._settings.EMBED_DIM),
                )
            return [e.values for e in resp.embeddings]

        vectors = await _with_retry(_call)
        return vectors[0]

    async def generate_json(self, *, model: str, system: str, prompt: str, schema: dict) -> dict:
        async def _call() -> dict:
            async with self._concurrency:
                resp = await self._client.aio.models.generate_content(
                    model=model,
                    contents=prompt,
                    config={
                        "system_instruction": system,
                        "response_mime_type": "application/json",
                        "response_json_schema": schema,
                    },
                )
            return json.loads(resp.text)

        return await _with_retry(_call)

    async def generate_stream(self, *, model: str, system: str, prompt: str) -> AsyncIterator[str]:
        attempt = 0
        delay = _BASE_DELAY_S
        yielded_any = False
        while True:
            attempt += 1
            try:
                async with self._concurrency:
                    stream = await self._client.aio.models.generate_content_stream(
                        model=model,
                        contents=prompt,
                        config={"system_instruction": system},
                    )
                async for chunk in stream:
                    if chunk.text:
                        yielded_any = True
                        yield chunk.text
                return
            except Exception as exc:  # noqa: BLE001 - reclassified immediately below
                classified = _classify(exc)
                # Once we've already streamed tokens to the caller, a retry would duplicate
                # them, so only retry failures that happen before anything was yielded.
                if yielded_any or isinstance(classified, PermanentError) or attempt >= _MAX_ATTEMPTS:
                    raise classified from exc
                logger.warning(
                    "generate_stream failed before any output (attempt %s/%s): %s",
                    attempt,
                    _MAX_ATTEMPTS,
                    classified,
                )
                await asyncio.sleep(delay * (1 + random.random() * 0.25))
                delay *= 2
