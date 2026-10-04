"""A dedicated test proving user B cannot read, retrieve from, delete, or
chat against user A's documents, threads, or files - even knowing A's exact
ids (BUILD_SPEC.md section 6.7). Foreign ids must come back as plain 404s,
never a 403 that would confirm the id exists for someone else.
"""

from conftest import CSRF_HEADERS
from pdf_fixtures import make_pdf_bytes
from test_chat_route import parse_sse

PASSWORD = "correct-horse-battery-staple"


def _signup(client, email):
    r = client.post("/api/auth/signup", json={"email": email, "password": PASSWORD}, headers=CSRF_HEADERS)
    assert r.status_code == 201
    return r.json()["id"]


def test_user_b_cannot_touch_user_as_documents_threads_or_files(client):
    # --- user A creates a document and a thread ---
    user_a_id = _signup(client, "isolation-a@example.com")
    pdf = make_pdf_bytes(["user a's private content"])
    up = client.post(
        "/api/documents", files=[("files", ("private.pdf", pdf, "application/pdf"))], headers=CSRF_HEADERS
    )
    assert up.status_code == 202
    doc_id = up.json()[0]["id"]

    chat_resp = client.post("/api/chat", json={"message": "hi"}, headers=CSRF_HEADERS)
    thread_id = next(d["thread_id"] for t, d in parse_sse(chat_resp.text) if t == "done")

    client.post("/api/auth/logout", headers=CSRF_HEADERS)

    # --- user B, who knows A's exact ids, tries everything ---
    _signup(client, "isolation-b@example.com")
    assert user_a_id  # sanity: B is a different account

    # Documents: read, file, delete
    assert client.get(f"/api/documents/{doc_id}").status_code == 404
    assert client.get(f"/api/documents/{doc_id}/file").status_code == 404
    assert client.delete(f"/api/documents/{doc_id}", headers=CSRF_HEADERS).status_code == 404
    assert doc_id not in {d["id"] for d in client.get("/api/documents").json()}

    # Chat: tagging A's document, or A's thread, by id
    r = client.post("/api/chat", json={"message": "what's in it?", "doc_ids": [doc_id]}, headers=CSRF_HEADERS)
    assert r.status_code == 404
    r = client.post("/api/chat", json={"message": "hi", "thread_id": thread_id}, headers=CSRF_HEADERS)
    assert r.status_code == 404

    # Threads: read, delete, list
    assert client.get(f"/api/threads/{thread_id}/messages").status_code == 404
    assert client.delete(f"/api/threads/{thread_id}", headers=CSRF_HEADERS).status_code == 404
    assert thread_id not in {t["id"] for t in client.get("/api/threads").json()}

    # Confirm the document and thread are untouched: A can still reach both.
    client.post("/api/auth/logout", headers=CSRF_HEADERS)
    client.post("/api/auth/login", json={"email": "isolation-a@example.com", "password": PASSWORD}, headers=CSRF_HEADERS)
    assert client.get(f"/api/documents/{doc_id}").status_code == 200
    assert client.get(f"/api/threads/{thread_id}/messages").status_code == 200
