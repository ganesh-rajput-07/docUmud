from tests.conftest import pdf_bytes

BOX = {"page": 0, "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.05, "label": "Invoice No", "value": "INV-1"}


def test_tag_crud(client, alice, upload):
    doc = upload(alice).json()
    url = f"/documents/{doc['id']}/tags"
    r = client.post(url, headers=alice, json=BOX)
    assert r.status_code == 201
    tag = r.json()

    r = client.patch(f"{url}/{tag['id']}", headers=alice, json={"label": "Date", "value": ""})
    assert r.json()["label"] == "Date" and r.json()["value"] == "" and r.json()["x"] == 0.1

    listed = client.get(url, headers=alice).json()
    assert listed["pages"] == 2 and [t["id"] for t in listed["tags"]] == [tag["id"]]

    assert client.delete(f"{url}/{tag['id']}", headers=alice).status_code == 204
    assert client.get(url, headers=alice).json()["tags"] == []
    assert client.delete(f"{url}/{tag['id']}", headers=alice).status_code == 404


def test_tag_validation(client, alice, upload):
    url = f"/documents/{upload(alice).json()['id']}/tags"
    assert client.post(url, headers=alice, json={**BOX, "page": 5}).status_code == 422
    assert client.post(url, headers=alice, json={**BOX, "x": 0.9, "w": 0.3}).status_code == 422
    assert client.post(url, headers=alice, json={**BOX, "w": 0}).status_code == 422
    assert client.post(url, headers=alice, json={**BOX, "label": ""}).status_code == 422
    tag = client.post(url, headers=alice, json=BOX).json()
    assert client.patch(f"{url}/{tag['id']}", headers=alice, json={"x": 0.95}).status_code == 422


def test_tag_isolation(client, alice, bob, upload):
    doc_id = upload(alice).json()["id"]
    url = f"/documents/{doc_id}/tags"
    tag = client.post(url, headers=alice, json=BOX).json()
    assert client.get(url, headers=bob).status_code == 404
    assert client.post(url, headers=bob, json=BOX).status_code == 404
    other = upload(bob).json()["id"]
    assert client.delete(f"/documents/{other}/tags/{tag['id']}", headers=bob).status_code == 404


def test_api_key_lifecycle(client, alice, bob, upload):
    r = client.post("/api-keys", headers=alice, json={"name": "ERP"})
    assert r.status_code == 201
    key, key_id = r.json()["key"], r.json()["id"]
    assert key.startswith("dm_")
    assert "key" not in client.get("/api-keys", headers=alice).json()[0]

    # header and bearer forms both work, and see the same account
    me = client.get("/auth/me", headers={"X-API-Key": key})
    assert me.status_code == 200
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {key}"}).json() == me.json()
    up = client.post("/documents", headers={"X-API-Key": key}, files={"file": ("a.pdf", pdf_bytes())})
    assert up.status_code == 201

    # keys can't mint or list keys; other users can't revoke
    assert client.post("/api-keys", headers={"X-API-Key": key}, json={"name": "x"}).status_code == 401
    assert client.delete(f"/api-keys/{key_id}", headers=bob).status_code == 404

    assert client.delete(f"/api-keys/{key_id}", headers=alice).status_code == 204
    assert client.get("/auth/me", headers={"X-API-Key": key}).status_code == 401
    assert client.get("/auth/me", headers={"X-API-Key": "dm_bogus"}).status_code == 401
    assert client.get("/auth/me").status_code == 401


def test_tagger_page(client):
    assert client.get("/documents/abc/tagger").status_code == 200
