import pytest

from app.llm import PermanentError, QuotaExhausted, TransientLLMError, _classify, _with_retry


class _FakeAPIError(Exception):
    def __init__(self, code: int, message: str = "boom"):
        super().__init__(message)
        self.code = code


async def test_with_retry_succeeds_after_two_429s(monkeypatch):
    monkeypatch.setattr("app.llm.asyncio.sleep", lambda *_a, **_k: _instant())

    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _FakeAPIError(429)
        return "ok"

    result = await _with_retry(flaky)
    assert result == "ok"
    assert calls["n"] == 3


async def test_with_retry_raises_quota_exhausted_when_retries_run_out(monkeypatch):
    monkeypatch.setattr("app.llm.asyncio.sleep", lambda *_a, **_k: _instant())

    async def always_429():
        raise _FakeAPIError(429)

    with pytest.raises(QuotaExhausted):
        await _with_retry(always_429, max_attempts=3)


async def test_with_retry_does_not_retry_permanent_errors(monkeypatch):
    calls = {"n": 0}

    async def bad_request():
        calls["n"] += 1
        raise _FakeAPIError(400, "invalid argument")

    with pytest.raises(PermanentError):
        await _with_retry(bad_request, max_attempts=4)
    assert calls["n"] == 1


def test_classify_maps_5xx_to_transient():
    assert isinstance(_classify(_FakeAPIError(503)), TransientLLMError)


def test_classify_maps_unknown_to_permanent():
    assert isinstance(_classify(ValueError("nope")), PermanentError)


async def _instant() -> None:
    return None
