from io import BytesIO

import pytest
from PIL import Image

from app.services.media import (
    MAX_COVER_BYTES,
    InvalidCoverImage,
    save_agent_chat_image,
    save_asset_image,
    save_project_cover,
    save_user_avatar,
)


def png_bytes():
    image = BytesIO()
    Image.new('RGB', (128, 96), '#226688').save(image, format='PNG')
    return image.getvalue()


def jpeg_bytes(size=(1200, 900), **options):
    image = BytesIO()
    Image.new('RGB', size, '#a94835').save(image, format='JPEG', **options)
    return image.getvalue()


def mpo_bytes(size=(1200, 900)):
    """A phone-camera JPEG: a JPEG carrying a second embedded picture."""
    first, second = BytesIO(), BytesIO()
    Image.new('RGB', size, '#a94835').save(first, format='JPEG', quality=90)
    Image.new('RGB', size, '#3550a9').save(second, format='JPEG', quality=90)
    payload = BytesIO()
    Image.open(BytesIO(first.getvalue())).save(
        payload, format='MPO', save_all=True,
        append_images=[Image.open(BytesIO(second.getvalue()))],
    )
    return payload.getvalue()


def heif_bytes():
    """An iPhone container Pillow cannot decode, only its header is needed."""
    return b'\x00\x00\x00\x20ftypheic\x00\x00\x00\x00heicmif1' + b'\x00' * 32


SAVE_HELPERS = {
    'asset image': (lambda data, root: save_asset_image(
        data, uploads_root=root, tenant_id='tenant', project_id='project', asset_id='asset')[1],
        (1200, 900)),
    'project cover': (lambda data, root: save_project_cover(
        data, uploads_root=root, tenant_id='tenant', project_id='project')[1],
        (1200, 900)),
    # Avatars are cropped to a fixed square rather than kept at source ratio.
    'user avatar': (lambda data, root: save_user_avatar(
        data, uploads_root=root, tenant_id='tenant', user_id='user')[1],
        (512, 512)),
    'agent chat image': (lambda data, root: save_agent_chat_image(
        data, uploads_root=root, tenant_id='tenant', project_id='project'),
        (1200, 900)),
}


@pytest.mark.parametrize('label', sorted(SAVE_HELPERS))
@pytest.mark.parametrize(
    'payload',
    [jpeg_bytes(progressive=True), jpeg_bytes(quality=100), mpo_bytes()],
    ids=['progressive-jpeg', 'jpeg-quality-100', 'phone-mpo-jpeg'],
)
def test_common_photo_encodings_are_accepted_everywhere(tmp_path, label, payload):
    # Phone cameras emit progressive JPEG and MPO (HDR/portrait/burst) files that
    # browsers still label image/jpeg; every upload surface must normalize them.
    save, expected_size = SAVE_HELPERS[label]
    with Image.open(save(payload, tmp_path)) as stored:
        assert stored.format == 'WEBP'
        assert stored.size == expected_size


@pytest.mark.parametrize('size', [(12000, 9000), (16384, 12288)])
def test_high_megapixel_phone_photo_is_downscaled_not_rejected(tmp_path, size):
    # 108MP and 200MP camera modes exceed the old 100MP bound while remaining
    # ordinary JPEGs that decode in bounded memory once sampling is applied.
    payload = jpeg_bytes(size, quality=80)
    assert size[0] * size[1] > 100_000_000
    _url, path = save_asset_image(payload, uploads_root=tmp_path, tenant_id='tenant',
                                  project_id='project', asset_id='asset')
    with Image.open(path) as stored:
        assert stored.format == 'WEBP'
        assert max(stored.size) == 4096


def test_oversized_lossless_image_keeps_the_stricter_limit(monkeypatch):
    # PNG has no sampling decoder, so it must stay bound to the stricter pixel
    # limit instead of being expanded to a full frame in memory.
    from app.services import media

    monkeypatch.setattr(media, 'MAX_UPLOAD_PIXELS_LOSSLESS', 100)
    monkeypatch.setattr(media.Image, 'MAX_IMAGE_PIXELS', None)
    with pytest.raises(InvalidCoverImage, match='像素过大'):
        media.validate_uploaded_image(png_bytes())


def test_heif_upload_reports_a_convertible_format_hint(tmp_path):
    with pytest.raises(InvalidCoverImage, match='HEIC'):
        save_agent_chat_image(heif_bytes(), uploads_root=tmp_path,
                              tenant_id='tenant', project_id='project')
    assert not list(tmp_path.rglob('*.webp'))


def test_unsupported_formats_are_still_rejected_with_the_format_message(tmp_path):
    for fmt in ('GIF', 'BMP', 'TIFF'):
        image = BytesIO()
        Image.new('RGB', (128, 96), '#226688').save(image, format=fmt)
        with pytest.raises(InvalidCoverImage, match='仅支持 JPG、PNG 或 WebP'):
            save_agent_chat_image(image.getvalue(), uploads_root=tmp_path,
                                  tenant_id='tenant', project_id='project')


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


@pytest.mark.parametrize(
    'payload',
    [jpeg_bytes(), jpeg_bytes(progressive=True), mpo_bytes()],
    ids=['jpeg', 'progressive-jpeg', 'phone-mpo-jpeg'],
)
def test_phone_jpeg_variants_upload_through_the_api(client, creator_headers, payload):
    # A phone camera JPEG must reach storage from every user-facing image route,
    # not just from the direct media helpers covered above.
    avatar = client.put('/api/v1/auth/me/avatar', headers=creator_headers,
        files={'file': ('PHOTO_0001.JPG', payload, 'image/jpeg')})
    assert avatar.status_code == 200, avatar.text
    with Image.open(BytesIO(client.get(avatar.json()['avatar_url']).content)) as saved:
        assert saved.format == 'WEBP'
        assert saved.size == (512, 512)

    attachment = client.post('/api/v1/agent/attachments', headers=creator_headers,
        files={'file': ('PHOTO_0002.JPG', payload, 'image/jpeg')})
    assert attachment.status_code == 201, attachment.text
    uploaded = client.get(attachment.json()['media_url'])
    assert uploaded.status_code == 200
    with Image.open(BytesIO(uploaded.content)) as saved:
        assert saved.format == 'WEBP'
    assert client.delete('/api/v1/agent/attachments/' + attachment.json()['id'],
        headers=creator_headers).status_code == 204


def test_high_megapixel_photo_uploads_through_the_api(client, creator_headers):
    # 108MP is the default output of current phone camera sensors.
    response = client.post('/api/v1/agent/attachments', headers=creator_headers,
        files={'file': ('IMG_9999.jpg', jpeg_bytes((12000, 9000), quality=80), 'image/jpeg')})
    assert response.status_code == 201, response.text
    stored = client.get(response.json()['media_url'])
    with Image.open(BytesIO(stored.content)) as saved:
        assert saved.format == 'WEBP'
        assert max(saved.size) == 4096
    assert client.delete('/api/v1/agent/attachments/' + response.json()['id'],
        headers=creator_headers).status_code == 204


def test_heif_upload_reports_the_convertible_format_message(client, creator_headers):
    response = client.post('/api/v1/agent/attachments', headers=creator_headers,
        files={'file': ('IMG_0001.HEIC', heif_bytes(), 'image/heic')})
    assert response.status_code == 422
    assert 'HEIC' in response.json()['detail']
