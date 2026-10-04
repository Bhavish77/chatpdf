import json

from conftest import CSRF_HEADERS

PASSWORD = "correct-horse-battery-staple"


def _signup(client, email):
    r = client.post("/api/auth/signup", json={"email": email, "password": PASSWORD}, headers=CSRF_HEADERS)
    assert r.status_code == 201
    return r.json()


def parse_sse(raw: str) -> list[tuple[str, dict]]:
    events = []
    for block in raw.strip().split("\n\n"):
        if not block.strip():
            continue
        event_type, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                event_type = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        if event_type:
            events.append((event_type, data))
    return events


def test_chat_with_no_documents_gives_friendly_message_without_calling_the_llm(client):
    _signup(client, "nodoc@example.com")
    r = client.post("/api/chat", json={"message": "hello?"}, headers=CSRF_HEADERS)
    assert r.status_code == 200
    events = parse_sse(r.text)
    types = [e[0] for e in events]
    assert "token" in types and "done" in types
    token_text = "".join(d["text"] for t, d in events if t == "token")
    assert "couldn't find" in token_text.lower()


def test_chat_with_unknown_doc_id_returns_404(client):
    _signup(client, "chatuser@example.com")
    r = client.post(
        "/api/chat", json={"message": "hi", "doc_ids": ["00000000-0000-0000-0000-000000000099"]}, headers=CSRF_HEADERS
    )
    assert r.status_code == 404


def test_chat_with_foreign_thread_id_returns_404(client):
    _signup(client, "threadowner@example.com")
    r = client.post("/api/chat", json={"message": "hi"}, headers=CSRF_HEADERS)
    thread_id = next(d["thread_id"] for t, d in parse_sse(r.text) if t == "done")
    client.post("/api/auth/logout", headers=CSRF_HEADERS)

    _signup(client, "threadintruder@example.com")
    r2 = client.post("/api/chat", json={"message": "hi", "thread_id": thread_id}, headers=CSRF_HEADERS)
    assert r2.status_code == 404


def test_thread_endpoints_are_isolated_per_user(client):
    _signup(client, "ta@example.com")
    r = client.post("/api/chat", json={"message": "hi"}, headers=CSRF_HEADERS)
    thread_id = next(d["thread_id"] for t, d in parse_sse(r.text) if t == "done")
    client.post("/api/auth/logout", headers=CSRF_HEADERS)

    _signup(client, "tb@example.com")
    assert client.get(f"/api/threads/{thread_id}/messages").status_code == 404
    assert client.delete(f"/api/threads/{thread_id}", headers=CSRF_HEADERS).status_code == 404
    assert thread_id not in {t["id"] for t in client.get("/api/threads").json()}
    client.post("/api/auth/logout", headers=CSRF_HEADERS)

    client.post("/api/auth/login", json={"email": "ta@example.com", "password": PASSWORD}, headers=CSRF_HEADERS)
    assert client.get(f"/api/threads/{thread_id}/messages").status_code == 200
    assert client.delete(f"/api/threads/{thread_id}", headers=CSRF_HEADERS).status_code == 200
