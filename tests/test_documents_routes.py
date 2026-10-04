import psycopg
from conftest import CSRF_HEADERS, override_env
from fastapi.testclient import TestClient
from pdf_fixtures import make_pdf_bytes

from app.config import get_settings
from app.main import app

PASSWORD = "correct-horse-battery-staple"


def _signup(client, email="docuser@example.com"):
    r = client.post("/api/auth/signup", json={"email": email, "password": PASSWORD}, headers=CSRF_HEADERS)
    assert r.status_code == 201
    return r.json()


def test_upload_creates_queued_documents_and_jobs(client):
    _signup(client)
    pdf = make_pdf_bytes(["hello world, this is page one of a real document"])
    txt = b"plain text content for upload, nothing fancy"

    r = client.post(
        "/api/documents",
        files=[
            ("files", ("a.pdf", pdf, "application/pdf")),
            ("files", ("b.txt", txt, "text/plain")),
        ],
        headers=CSRF_HEADERS,
    )
    assert r.status_code == 202
    docs = r.json()
    assert len(docs) == 2
    assert {d["filename"] for d in docs} == {"a.pdf", "b.txt"}
    assert all(d["status"] == "queued" for d in docs)
    assert all(d["progress"] == 0 for d in docs)

    settings = get_settings()
    with psycopg.connect(settings.DATABASE_URL, autocommit=True) as conn:
        cur = conn.execute("select count(*) from jobs where kind = 'ingest_document'")
        assert cur.fetchone()[0] == 2


def test_upload_rejects_wrong_extension(client):
    _signup(client)
    r = client.post(
        "/api/documents",
        files=[("files", ("a.exe", b"MZ\x90\x00fake", "application/octet-stream"))],
        headers=CSRF_HEADERS,
    )
    assert r.status_code == 415


def test_upload_rejects_mismatched_magic_bytes(client):
    _signup(client)
    r = client.post(
        "/api/documents",
        files=[("files", ("a.pdf", b"not actually a pdf", "application/pdf"))],
        headers=CSRF_HEADERS,
    )
    assert r.status_code == 415


def test_upload_rejects_oversized_file():
    with override_env(MAX_FILE_MB="1"), TestClient(app) as c:
        _signup(c)
        big = b"%PDF-1.4\n" + b"0" * 2_000_000  # magic bytes + padding past the 1 MB cap
        r = c.post(
            "/api/documents",
            files=[("files", ("big.pdf", big, "application/pdf"))],
            headers=CSRF_HEADERS,
        )
        assert r.status_code == 413


def test_list_documents_shows_only_own_plus_public(client):
    _signup(client, email="a@example.com")
    pdf = make_pdf_bytes(["doc for user a"])
    client.post(
        "/api/documents", files=[("files", ("mine.pdf", pdf, "application/pdf"))], headers=CSRF_HEADERS
    )
    client.post("/api/auth/logout", headers=CSRF_HEADERS)

    _signup(client, email="b@example.com")
    r = client.get("/api/documents")
    filenames = {d["filename"] for d in r.json()}
    assert "mine.pdf" not in filenames


def test_foreign_document_returns_404(client):
    _signup(client, email="owner@example.com")
    pdf = make_pdf_bytes(["owner's document"])
    r = client.post(
        "/api/documents", files=[("files", ("owner.pdf", pdf, "application/pdf"))], headers=CSRF_HEADERS
    )
    doc_id = r.json()[0]["id"]
    client.post("/api/auth/logout", headers=CSRF_HEADERS)

    _signup(client, email="intruder@example.com")
    assert client.get(f"/api/documents/{doc_id}").status_code == 404
    assert client.get(f"/api/documents/{doc_id}/file").status_code == 404
    assert client.delete(f"/api/documents/{doc_id}", headers=CSRF_HEADERS).status_code == 404


def test_delete_removes_document_and_blob(client):
    _signup(client)
    pdf = make_pdf_bytes(["delete me"])
    r = client.post(
        "/api/documents", files=[("files", ("gone.pdf", pdf, "application/pdf"))], headers=CSRF_HEADERS
    )
    doc_id = r.json()[0]["id"]

    assert client.delete(f"/api/documents/{doc_id}", headers=CSRF_HEADERS).status_code == 200
    assert client.get(f"/api/documents/{doc_id}").status_code == 404

    settings = get_settings()
    with psycopg.connect(settings.DATABASE_URL, autocommit=True) as conn:
        cur = conn.execute("select count(*) from blobs where document_id = %s", (doc_id,))
        assert cur.fetchone()[0] == 0


def test_get_document_file_serves_pdf_inline(client):
    _signup(client)
    pdf = make_pdf_bytes(["inline pdf content"])
    r = client.post(
        "/api/documents", files=[("files", ("view.pdf", pdf, "application/pdf"))], headers=CSRF_HEADERS
    )
    doc_id = r.json()[0]["id"]

    r = client.get(f"/api/documents/{doc_id}/file")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/pdf")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "inline" in r.headers["content-disposition"]
