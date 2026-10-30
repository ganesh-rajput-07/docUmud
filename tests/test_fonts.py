from pathlib import Path

FONT = Path(__file__).parent.parent / "app/static/fonts/Allura.ttf"


def _up(client, headers, data=None, name="allura.ttf", **form):
    return client.post("/fonts", headers=headers, data=form,
                       files={"file": (name, FONT.read_bytes() if data is None else data)})


def test_font_lifecycle_and_isolation(client, alice, bob):
    r = _up(client, alice, name="My Hand.ttf")
    assert r.status_code == 201 and r.json()["name"] == "My Hand"
    fid = r.json()["id"]
    assert [f["id"] for f in client.get("/fonts", headers=alice).json()] == [fid]
    got = client.get(f"/fonts/{fid}/file", headers=alice)
    assert got.status_code == 200 and got.content == FONT.read_bytes() and got.headers["content-type"] == "font/ttf"

    assert client.get("/fonts", headers=bob).json() == []
    assert client.get(f"/fonts/{fid}/file", headers=bob).status_code == 404
    assert client.get(f"/fonts/{fid}/file").status_code == 401
    assert client.delete(f"/fonts/{fid}", headers=bob).status_code == 404
    assert client.delete(f"/fonts/{fid}", headers=alice).status_code == 204
    assert client.get(f"/fonts/{fid}/file", headers=alice).status_code == 404


def test_font_validation(client, alice, monkeypatch):
    assert _up(client, alice, b"not a font at all").status_code == 422
    assert _up(client, alice, b"\x00\x01\x00\x00" + b"junk" * 50).status_code == 422  # right magic, corrupt
    assert _up(client, alice, b"<script>alert(1)</script>", name="x.ttf").status_code == 422
    assert _up(client, alice, b"").status_code == 400
    from app.config import settings
    monkeypatch.setattr(settings, "max_font_kb", 10)
    assert _up(client, alice).status_code == 413
    monkeypatch.setattr(settings, "max_font_kb", 2048)
    monkeypatch.setattr(settings, "max_user_fonts", 1)
    assert _up(client, alice).status_code == 201
    assert _up(client, alice).status_code == 409
