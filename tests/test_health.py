def test_health_ok(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["db"] == "ok"
    assert body["gemini_key"] == "present"
    assert "generation" in body["models"]


def test_config_public(client):
    response = client.get("/api/config")
    assert response.status_code == 200
    body = response.json()
    assert body["signups_enabled"] is True
    assert body["invite_required"] is False
    assert set(body["allowed_types"]) == {"pdf", "docx", "txt", "md"}
