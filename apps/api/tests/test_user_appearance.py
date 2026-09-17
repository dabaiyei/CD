def test_background_blur_persists_and_is_private(client, creator_headers, admin_headers):
    path = "/api/v1/auth/me/appearance"
    assert client.get("/api/v1/auth/me", headers=creator_headers).json()["user"]["background_blur"] == 0
    response = client.patch(path, headers=creator_headers, json={"background_blur": 18})
    assert response.status_code == 200
    assert response.json()["background_blur"] == 18
    assert client.get("/api/v1/auth/me", headers=creator_headers).json()["user"]["background_blur"] == 18
    assert client.get("/api/v1/auth/me", headers=admin_headers).json()["user"]["background_blur"] == 0
    for value in (-1, 31, 1.5, "12", True):
        assert client.patch(path, headers=creator_headers, json={"background_blur": value}).status_code == 422
    assert client.patch(path, headers=creator_headers, json={"background_blur": 5, "user_id": "other"}).status_code == 422
    assert client.patch(path, json={"background_blur": 5}).status_code == 401
    assert client.patch(path, headers=creator_headers, json={"background_blur": 0}).json()["background_blur"] == 0
