from io import BytesIO

import pytest
from PIL import Image

from app.services.media import MAX_COVER_BYTES, InvalidCoverImage, save_agent_chat_image


def png_bytes():
    image = BytesIO()
    Image.new('RGB', (128, 96), '#226688').save(image, format='PNG')
    return image.getvalue()


@pytest.mark.parametrize('size', [9 * 1024 * 1024, 100 * 1024 * 1024])
def test_upload_large_image_with_generic_mime_is_normalized(client, creator_headers, size):
    # Legal PNG with trailing padding exercises the byte limit without needing
    # hundreds of millions of decoded pixels or slow random-image encoding.
    payload = png_bytes().ljust(size, b'\0')
    response = client.put('/api/v1/auth/me/avatar', headers=creator_headers,
        files={'file': ('photo.png', payload, 'application/octet-stream')})
    assert response.status_code == 200, response.text
    result = client.get(response.json()['avatar_url'])
    assert len(result.content) < size
    with Image.open(BytesIO(result.content)) as saved:
        assert saved.format == 'WEBP'
        assert saved.size == (512, 512)


def test_upload_over_100mb_is_rejected(client, creator_headers):
    response = client.put('/api/v1/auth/me/avatar', headers=creator_headers,
        files={'file': ('large.png', png_bytes().ljust(MAX_COVER_BYTES + 1, b'\0'), 'image/png')})
    assert response.status_code == 413
    assert '100' in response.json()['detail']


def test_chat_reference_preserves_ratio_and_alpha_in_webp(tmp_path):
    image = BytesIO()
    Image.new('RGBA', (4200, 2100), (20, 60, 100, 128)).save(image, format='PNG')
    path = save_agent_chat_image(image.getvalue(), uploads_root=tmp_path,
        tenant_id='tenant', project_id='project')
    with Image.open(path) as normalized:
        assert normalized.format == 'WEBP'
        assert normalized.size == (4096, 2048)
        assert normalized.getpixel((0, 0))[3] == 128


def test_fake_image_is_not_accepted_by_filename(tmp_path):
    with pytest.raises(InvalidCoverImage, match='无效或已损坏'):
        save_agent_chat_image(b'not an image', uploads_root=tmp_path,
            tenant_id='tenant', project_id='project')
    assert not list(tmp_path.rglob('*.webp'))


def test_personal_chat_upload_returns_accessible_webp(client, creator_headers):
    response = client.post('/api/v1/agent/attachments', headers=creator_headers,
        files={'file': ('reference.png', png_bytes(), 'image/png')})
    assert response.status_code == 201, response.text
    result = client.get(response.json()['media_url'])
    assert result.status_code == 200
    with Image.open(BytesIO(result.content)) as saved:
        assert saved.format == 'WEBP'
        assert saved.size == (128, 96)
    assert client.delete('/api/v1/agent/attachments/' + response.json()['id'],
        headers=creator_headers).status_code == 204


def test_browser_webp_preserved_and_second_upload_independent(client, creator_headers):
    data = BytesIO()
    Image.new('RGBA', (128, 96), (20, 60, 100, 128)).save(data, format='WEBP', quality=90)
    ids = []
    for _ in range(2):
        response = client.post('/api/v1/agent/attachments', headers=creator_headers,
            files={'file': ('reference.webp', data.getvalue(), 'image/webp')})
        assert response.status_code == 201, response.text
        ids.append(response.json()['id'])
        result = client.get(response.json()['media_url'])
        assert result.content == data.getvalue()
    assert ids[0] != ids[1]
    for attachment_id in ids:
        assert client.delete('/api/v1/agent/attachments/' + attachment_id,
            headers=creator_headers).status_code == 204
