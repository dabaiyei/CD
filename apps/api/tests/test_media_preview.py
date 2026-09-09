from io import BytesIO
from uuid import uuid4

from PIL import Image

from app.core.config import get_settings
from app.services.object_storage import media_signature


def test_thumbnail_keeps_signature_policy_and_original(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "require_signed_media_urls", True)
    key = f"preview-tests/{uuid4().hex}.png"
    source = settings.uploads_root / key
    source.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (2048, 1024), "red").save(source)
    original = source.read_bytes()
    assert client.get(f"/uploads/{key}?thumbnail=320").status_code == 403
    url = f"/uploads/{key}?signature={media_signature(key)}&thumbnail=320"
    response = client.get(url)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/webp"
    with Image.open(BytesIO(response.content)) as preview:
        assert preview.size == (320, 160)
    assert client.get(url).content == response.content
    assert source.read_bytes() == original
    assert client.get(url.replace("thumbnail=320", "thumbnail=500")).status_code == 422
