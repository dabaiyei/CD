import asyncio
import json
from array import array

import pytest

from app.services.canvas_media import process, probe
from app.services.video_concat import media_binary, run_media_command
from test_canvas_video_reading import upload_video


@pytest.fixture
def media_video(tmp_path):
    if not media_binary('ffmpeg') or not media_binary('ffprobe'):
        pytest.skip('FFmpeg is unavailable')
    path = tmp_path / 'video.mp4'
    asyncio.run(run_media_command('ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=red:s=96x64:r=12:d=2',
        '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2', '-vf', "drawbox=x=0:y=0:w=iw:h=ih:color=blue:t=fill:enable='gte(t,1)'",
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(path)))
    return path


def packet_hashes(path):
    data = asyncio.run(run_media_command('ffprobe', '-v', 'error', '-select_streams', 'v', '-show_packets',
        '-show_data_hash', 'sha256', '-show_entries', 'packet=data_hash,pts_time', '-of', 'json', str(path)))
    return json.loads(data)['packets']


@pytest.mark.parametrize('format,length', [('wav', 0.6), ('mp3', 4), ('flac', 2)])
def test_mux_replaces_audio_preserves_video_packets_and_pads_or_truncates(tmp_path, media_video, format, length):
    audio = tmp_path / ('audio.' + format)
    asyncio.run(run_media_command('ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', f'sine=frequency=880:duration={length}', str(audio)))
    result = asyncio.run(process(media_video.read_bytes(), operation='mux_audio', audio=audio.read_bytes(), audio_start=0.1, offset=0.3))
    output = tmp_path / 'mux.mp4'
    output.write_bytes(result['data'])
    info = asyncio.run(probe(output))
    assert len([s for s in info['streams'] if s['codec_type'] == 'audio']) == 1
    assert packet_hashes(output) == packet_hashes(media_video)
    assert result['duration'] == pytest.approx(2, abs=0.001)
    assert float(info['format']['duration']) == pytest.approx(2, abs=0.03)
    pcm = asyncio.run(run_media_command('ffmpeg', '-v', 'error', '-i', str(output), '-map', '0:a:0', '-ac', '1', '-ar', '8000', '-f', 's16le', '-'))
    samples = array('h', pcm)
    assert max(abs(x) for x in samples[:1600]) < 10
    assert max(abs(x) for x in samples[3200:4000]) > 1000
    if length < 2:
        assert max(abs(x) for x in samples[12000:15000]) < 10


def test_exact_audio_range_and_video_segment(tmp_path, media_video):
    result = asyncio.run(process(media_video.read_bytes(), operation='extract_audio', start=0.5, end=1.25))
    assert result['mime_type'] == 'audio/mp4'
    assert result['duration'] == pytest.approx(0.75, abs=0.03)
    assert (result['source_start'], result['source_end']) == (0.5, 1.25)
    clip = asyncio.run(process(media_video.read_bytes(), operation='trim_video', start=0.5, end=1.5))
    assert clip['duration'] == pytest.approx(1, abs=0.02)
    assert (clip['width'], clip['height']) == (96, 64)
    from app.services.canvas_video_reading import extract
    from PIL import Image
    from io import BytesIO
    frames = asyncio.run(extract(clip['data'], count=2, sampling='uniform'))['frames']
    for frame in frames:
        with Image.open(BytesIO(frame['data'])) as image:
            red, _, blue = image.convert('RGB').getpixel((20, 20))
            assert (red > blue) == (frame['at'] < 0.5)
    with pytest.raises(ValueError, match='范围'):
        asyncio.run(process(media_video.read_bytes(), operation='trim_video', start=1, end=3))
    with pytest.raises(ValueError, match='音轨'):
        asyncio.run(process(media_video.read_bytes(), operation='mux_audio'))


@pytest.mark.parametrize('codec,container', [('libvpx', 'webm'), ('libx264', 'mkv')])
def test_mux_accepts_webm_and_uses_picture_duration_not_long_container_audio(tmp_path, codec, container):
    source = tmp_path / ('source.' + container)
    audio = tmp_path / 'short.wav'
    asyncio.run(run_media_command('ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=red:s=96x64:r=12:d=2',
        '-f', 'lavfi', '-i', 'sine=frequency=440:duration=4', '-c:v', codec, '-c:a', 'libopus', str(source)))
    assert float(asyncio.run(probe(source))['format']['duration']) > 3.9
    asyncio.run(run_media_command('ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=880:duration=1', str(audio)))
    result = asyncio.run(process(source.read_bytes(), operation='mux_audio', audio=audio.read_bytes()))
    assert result['duration'] == pytest.approx(2, abs=0.03)
    assert result['width'] == 96 and result['height'] == 64


def test_media_api_auth_ownership_and_derivatives(client, creator_headers, admin_headers, media_video):
    data = media_video.read_bytes()
    key = upload_video(client, creator_headers, data, 'video:media-process')
    payload = {'operation': 'extract_audio', 'video_key': key}
    assert client.post('/api/v1/canvas/media-process', json=payload).status_code == 401
    assert client.post('/api/v1/canvas/media-process', headers=admin_headers, json=payload).status_code == 422
    extracted = client.post('/api/v1/canvas/media-process', headers=creator_headers, json=payload)
    assert extracted.status_code == 200, extracted.text
    audio_key = extracted.json()['storageKey']
    payload = {'operation': 'mux_audio', 'video_key': key, 'audio_key': audio_key}
    assert client.post('/api/v1/canvas/media-process', headers=creator_headers, json={**payload, 'audio_key': key}).status_code == 422
    muxed = client.post('/api/v1/canvas/media-process', headers=creator_headers, json=payload)
    assert muxed.status_code == 200, muxed.text
    assert muxed.json()['storageKey'] != key and muxed.json()['duration'] == pytest.approx(2)
    original = '/api/v1/canvas/storage/infinite-canvas.media_files/' + key
    assert client.get(original, headers=creator_headers).content == data
    audio_url = '/api/v1/canvas/storage/infinite-canvas.media_files/' + audio_key
    assert client.get(audio_url, headers=creator_headers).headers['content-type'] == 'audio/mp4'
    assert client.get(audio_url, headers=admin_headers).status_code == 204
    assert client.post('/api/v1/canvas/media-process', headers=creator_headers, json={**payload, 'audio_start': -1}).status_code == 422
