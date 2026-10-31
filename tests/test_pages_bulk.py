def test_new_pages_render(client):
    for path in ("/settings/email", "/documents/x/bulk", "/sends/x"):
        assert client.get(path).status_code == 200
