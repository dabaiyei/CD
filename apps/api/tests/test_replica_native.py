import asyncio
import io
import json
import os
import re
import zipfile
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db.models import AITask, TaskStatus, User
from app.db.session import SessionLocal
from app.services import hypit_bridge, replica_production, replica_studio
from app.services.object_storage import persist_media_file
from app.services.task_worker import process_task
from app.services.video_concat import media_binary, run_media_command
from app.services.video_replica import RENDER


def test_native_sources_and_proxy_path_boundaries(tmp_path):
    clip = tmp_path / "input.mp4"
    clip.write_bytes(b"fixture")
    root = tmp_path / "project"
    clips = [{"path": str(clip), "key": "owned-key", "duration": 1}]
    hypit_bridge.write_composition(root, clips, width=160, height=90)
    value = replica_production.capture(root, clips, {})
    replica_production.validate_sources(value["files"], value["assets"])
    for replacement in ['"https://example.com/video.mp4"', '"../../secret"', "{remote.path}"]:
        bad = {
            **value["files"],
            "composition.svml": value["files"]["composition.svml"].replace(
                '"./assets/clip-0.mp4"', replacement
            ),
        }
        with pytest.raises(ValueError):
            replica_production.validate_sources(bad, value["assets"])
    bad = {
        **value["files"],
        "composition.svml": value["files"]["composition.svml"].replace("@hypit/media@1", "@evil/code@1"),
    }
    with pytest.raises(ValueError):
        replica_production.validate_sources(bad, value["assets"])
    studio = SimpleNamespace(root=root)
    assert not replica_studio.permitted_path("%2e%2e/secrets", studio)
    assert not replica_studio.permitted_path("@fs/C:/Code/CD/.env", studio)
    source = 'import "/src/ui/main.ts"; fetch("/__studio/session"); a.split("/"); a === "/>"'
    rebased = replica_studio.rewrite(source, "/scoped")
    assert '"/scoped/src/ui/main.ts"' in rebased and '"/scoped/__studio/session"' in rebased
    assert 'split("/")' in rebased and '=== "/>"' in rebased
    localized = replica_studio.rewrite('import en from "/locales/en.json?import";', "/scoped")
    assert '"/scoped/locales/en.json?import"' in localized


def test_native_editor_export_reuses_media_and_isolates_accounts(
    client, admin_headers, creator_headers, tmp_path
):
    if not hypit_bridge.available() or not media_binary("ffmpeg"):
        pytest.skip("Installed Hypit and FFmpeg required for native integration")

    async def setup():
        from app.core.config import get_settings

        async with SessionLocal() as db:
            user = await db.scalar(select(User).where(User.email == "admin@cineforge.local"))
            path = get_settings().uploads_root / user.tenant_id / user.id / "native-test.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            await run_media_command(
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=blue:s=160x90:r=30:d=1",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=stereo",
                "-t",
                "1",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                str(path),
            )
            key, _ = await persist_media_file(path, "video/mp4")
            clips = [{"path": str(path), "key": key, "duration": 1}]
            root = tmp_path / "initial"
            hypit_bridge.write_composition(root, clips, width=160, height=90)
            value = replica_production.capture(root, clips, {"BRIEF.md": "test native production"})
            task = AITask(
                tenant_id=user.tenant_id,
                user_id=user.id,
                task_type=RENDER,
                status=TaskStatus.SUCCEEDED,
                cost=0,
                request_payload={},
                result_payload={"production": value},
            )
            db.add(task)
            await db.commit()
            return task.id, value

    task, value = asyncio.run(setup())
    base = f"/api/v1/video-replicas/{task}"
    assert client.get(base + "/production", headers=creator_headers).status_code == 404
    assert client.post(base + "/production/check", headers=admin_headers).status_code == 200
    opened = client.post(base + "/studio", headers=admin_headers)
    assert opened.status_code == 200, opened.text
    session = opened.json()
    proxy = session["url"].split("?")[0]
    try:
        assert client.get(proxy + "__studio/session").status_code == 401
        page = client.get(session["url"])
        assert page.status_code == 200, page.text
        assert proxy + "src/ui/main.ts" in page.text
        native = client.get(proxy + "__studio/session")
        assert native.status_code == 200, native.text
        snapshot = native.json()
        assert snapshot["tracks"], snapshot
        script = client.get(proxy + "src/ui/main.ts")
        assert script.status_code == 200, script.text[:1000]
        vite = client.get(proxy + "@vite/client")
        token = re.search(r'const wsToken = "([^"]+)"', vite.text)
        assert token, "Native Studio must expose its scoped HMR handshake"
        with client.websocket_connect(
            proxy + "socket?token=" + token[1], subprotocols=["vite-hmr"]
        ) as socket:
            assert socket.receive_json()["type"] == "connected"
        # Native source editing and source revision handling, without a replacement UI.
        changed = value["files"]["look.svs"].replace("#000000", "#102030")
        response = client.put(
            proxy + "__studio/source",
            json={"path": "look.svs", "text": changed, "revision": snapshot["revision"]},
        )
        assert response.status_code == 202, response.text
        saved = client.post(base + f"/studio/{session['id']}/save", headers=admin_headers)
        assert saved.status_code == 200, saved.text
        value = saved.json()
        assert "#102030" in value["files"]["look.svs"]
        unsafe = client.put(
            proxy + "__studio/source", json={"path": "../../.env", "text": "x", "revision": 1}
        )
        assert unsafe.status_code == 422
        assert client.get(proxy + "@fs/C:/Code/CD/.env").status_code == 403
        assert client.post(base + f"/studio/{session['id']}/save", headers=creator_headers).status_code == 404
    finally:
        client.delete(base + f"/studio/{session['id']}", headers=admin_headers)
    stale = client.put(
        base + "/production", headers=admin_headers, json={"revision": "0" * 64, "files": value["files"]}
    )
    assert stale.status_code == 409
    archive = client.get(base + "/production/archive", headers=admin_headers)
    assert archive.status_code == 200, archive.text if archive.status_code != 200 else ""
    with zipfile.ZipFile(io.BytesIO(archive.content)) as zipped:
        assert "assets/clip-0.mp4" in zipped.namelist()
        assert "HYPIT-LICENSE.txt" in zipped.namelist()
        assert json.loads(zipped.read("hypit.runtime.json"))["credentials"] == {}
    export = client.post(
        base + "/production/export", headers=admin_headers, json={"revision": value["revision"]}
    )
    assert export.status_code == 202, export.text
    export_id = export.json()["id"]

    def forbidden(*args, **kwargs):
        raise AssertionError("Local production export must not invoke any AI model")

    asyncio.run(process_task(export_id, runtime_factory=forbidden, gateway_factory=forbidden))
    done = client.get(f"/api/v1/tasks/{export_id}", headers=admin_headers).json()
    assert done["status"] == "succeeded", done
    assert done["result_payload"]["media_url"]
    assert done["result_payload"]["production"]["assets"] == value["assets"]

    # AI edits are compiled locally; an invalid draft repairs only the Source,
    # with no image/video provider call and without losing frozen media.
    from app.db.models import AIModel, ModelType
    from app.services.agent_runtime import AgentRuntimeResponse
    from app.services.media_gateway import SpeechGenerationResult

    async def models():
        async with SessionLocal() as db:
            text = await db.scalar(select(AIModel).where(AIModel.model_type == ModelType.TEXT))
            speech = AIModel(
                tenant_id=text.tenant_id,
                provider_id=text.provider_id,
                model_type=ModelType.TTS,
                model_id="native-test-tts",
                name="Native test voice",
                enabled=True,
                capabilities={"voices": ["test"]},
            )
            db.add(speech)
            await db.commit()
            return text.id, speech.id

    text_id, speech_id = asyncio.run(models())
    calls = []

    class Runtime:
        async def run(self, request):
            assert request.tool_mode == "none" and not request.skills
            calls.append(request.prompt)
            changed = value["files"]["look.svs"].replace("#102030", "#203040")
            if len(calls) == 1:
                changed = "<invalid>"
            return AgentRuntimeResponse(
                session_id=request.session_id,
                final_response=json.dumps({"files": {"look.svs": changed}}),
                finish_reason="completed",
                events=[],
                manifest={},
            )

    edit = client.post(
        base + "/production/edit",
        headers=admin_headers,
        json={
            "revision": value["revision"],
            "instruction": "修改背景颜色",
            "text_model_id": text_id,
            "components": ["media-track"],
        },
    )
    assert edit.status_code == 202, edit.text
    edit_id = edit.json()["id"]
    asyncio.run(process_task(edit_id, runtime_factory=Runtime, gateway_factory=forbidden))
    edited = client.get(f"/api/v1/tasks/{edit_id}", headers=admin_headers).json()
    assert edited["status"] == "succeeded", edited
    assert len(calls) == 2 and "上次修改没有通过" in calls[-1]
    assert edited["result_payload"]["production"]["assets"] == value["assets"]

    audio = tmp_path / "voice.wav"
    asyncio.run(
        run_media_command(
            "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(audio)
        )
    )
    submits = []

    class Gateway:
        async def submit_speech(self, request):
            submits.append(request)
            return SpeechGenerationResult(
                status="succeeded", audio_data=audio.read_bytes(), content_type="audio/wav"
            )

    speech_body = {
        "revision": value["revision"],
        "speech": {"model_id": speech_id, "voice": "test", "text": "本地配音测试"},
    }
    speech = client.post(base + "/production/speech", headers=admin_headers, json=speech_body)
    assert speech.status_code == 202, speech.text
    speech_task = speech.json()["id"]
    assert (
        client.post(base + "/production/speech", headers=creator_headers, json=speech_body).status_code == 404
    )
    asyncio.run(process_task(speech_task, runtime_factory=forbidden, gateway_factory=lambda _: Gateway()))
    voiced = client.get(f"/api/v1/tasks/{speech_task}", headers=admin_headers).json()
    assert voiced["status"] == "succeeded", voiced
    assert len(submits) == 1 and submits[0].text == "本地配音测试"
    assert len(voiced["result_payload"]["production"]["assets"]) == len(value["assets"]) + 1
    assert voiced["result_payload"]["production"]["files"] == value["files"]
    assert (
        client.post(base + "/production/speech", headers=admin_headers, json=speech_body).json()["id"]
        == speech_task
    )


@pytest.mark.skipif(
    os.environ.get("HYPIT_TEST_SPEECH") != "1", reason="Opt-in real local WhisperX integration"
)
def test_native_semantic_caption_render(client, admin_headers, tmp_path):
    from app.core.config import PROJECT_ROOT, get_settings
    from app.services import replica_transcription
    from app.services.video_replica import EXPORT

    async def setup():
        speech = PROJECT_ROOT / "runtime-data/speech-test/en.wav"
        if not speech.exists():
            pytest.skip("Local speech verification fixture is absent")

        async def pulse(*args):
            pass

        transcript = await replica_transcription.transcribe(speech, tmp_path, "en", pulse)
        text = " ".join(p["text"] for p in transcript["passages"])
        assert text.strip()
        assert any(p.get("words") for p in transcript["passages"])
        text = re.sub(r"([<>@{}|\\])", r"\\\1", text)
        async with SessionLocal() as db:
            user = await db.scalar(select(User).where(User.email == "admin@cineforge.local"))
            path = get_settings().uploads_root / user.tenant_id / user.id / "native-speech-test.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            await run_media_command(
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=blue:s=320x180:r=30",
                "-i",
                str(speech),
                "-shortest",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                str(path),
            )
            info = await hypit_bridge.command("media", "probe", str(path), "--json", workspace=tmp_path)
            key, _ = await persist_media_file(path, "video/mp4")
            clips = [{"path": str(path), "key": key, "duration": info["duration"]}]
            root = tmp_path / "semantic"
            hypit_bridge.write_composition(root, clips, width=320, height=180)
            value = replica_production.capture(root, clips, {})
            source = value["files"]["composition.svml"]
            source = source.replace(
                "<svml>",
                '<svml><import from="@hypit/script@1"/>'
                '<import as="whisperx" from="@hypit/whisperx@1"/>'
                '<import as="caption" from="@hypit/caption-fine@1"/>'
                '<import as="fonts" from="@hypit/fonts-open@1"/>',
            )
            source = re.sub(
                r"<time:Timeline[^>]+/>",
                lambda _: (
                    f'<script id="story"><speech>{text}</speech></script>'
                    '<whisperx:SemanticTake id="spoken" narrative={story} '
                    'segment={story.segment.speech} media={media-0.media} language="en"/>'
                    f'<time:Timeline id="timeline" clock={{clock}} end="{round(info["duration"] * 30)}f">'
                    '<time:Take source={spoken.take}/></time:Timeline>'
                    '<fonts:Stack id="font" family="inter" weight="400" style="normal"/>'
                    '<caption:Style id="caption-style" recipe={look.caption.primary} font={font}/>'
                    '<caption:Track id="captions" document={story.caption} timeline={timeline.timeline}>'
                    "<caption:Use style={caption-style}/></caption:Track>"
                ),
                source,
            )
            source = source.replace("</film:Film>", "<film:Track source={captions.track}/></film:Film>")
            value["files"]["composition.svml"] = source
            value["files"]["look.svs"] = value["files"]["look.svs"].replace(
                "</sheet>",
                "caption.primary { stack-order: 70; x: 0.5; y: 0.9; width: 0.9; height: 0.8; "
                "anchor-x: center; anchor-y: bottom; align: center; block-align: end; "
                "wrap: word; size: 20; line-height: 1.2; fill: #FFFFFF; karaoke: current; "
                'active-fill: #FFD54A; background: #00000000; padding: "0"; radius: 0; }\n</sheet>',
            )
            task = AITask(
                tenant_id=user.tenant_id,
                user_id=user.id,
                task_type=EXPORT,
                cost=0,
                request_payload={"production": value},
            )
            db.add(task)
            await db.commit()
            return task.id

    task_id = asyncio.run(setup())
    asyncio.run(process_task(task_id))
    result = client.get(f"/api/v1/tasks/{task_id}", headers=admin_headers).json()
    assert result["status"] == "succeeded", result
    assert result["result_payload"]["media_url"]
