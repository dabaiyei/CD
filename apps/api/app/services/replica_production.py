"""Editable native Hypit productions with immutable, account-scoped media inputs.

Generation stays behind the application's model adapters. Editing a native Source
only runs local Hypit components, and export never submits a generation request.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.core.security import SecretBox
from app.db.models import ModelType
from app.db.session import SessionLocal
from app.services import hypit_bridge
from app.services.object_storage import materialize_media_file, persist_media_file

SOURCE_FILES = ("composition.svml", "look.svs", "render.svrun")
DOCUMENT_FILES = (
    "BRIEF.md",
    "ANALYSIS.md",
    "TREATMENT.md",
    "TIMELINE.md",
    "PROGRESS.md",
    "TRANSCRIPT.md",
    "FEEDBACK.json",
)
# Local authoring only: generation is deliberately routed through existing authenticated adapters.
PACKAGES = frozenset(
    {
        "media",
        "media-pipeline",
        "timeline-author",
        "spatial",
        "media-track",
        "audio-track",
        "typography-track",
        "text",
        "fonts-open",
        "caption-fine",
        "caption",
        "script",
        "narrative",
        "performance",
        "sound",
        "film",
        "render-hyperframes",
        "motion",
        "paint",
        "screen-overlay",
        "deck-track",
        "ranking",
        "comment-sticker",
        "whisperx",
    }
)


def revision(files):
    return hashlib.sha256(json.dumps(files, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def capture(root, clips, documents):
    files = {name: (root / name).read_text(encoding="utf-8") for name in SOURCE_FILES}
    files.update({name: str(content) for name, content in documents.items() if name in DOCUMENT_FILES})
    for name in DOCUMENT_FILES:
        if name != "FEEDBACK.json":
            files.setdefault(name, "")
    files["PROGRESS.md"] = "素材生成完成。可编辑同一工程的编排、文字、样式与声音；重新导出复用现有素材。"
    manifest = {
        f"assets/clip-{i}.mp4": {"key": clip["key"], "duration": clip["duration"], "type": "video"}
        for i, clip in enumerate(clips)
    }
    return {"version": 1, "files": files, "assets": manifest, "revision": revision(files)}


class SourceEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    files: dict[str, str]


class SpeechInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_id: str
    text: str = Field(min_length=1, max_length=6000)
    voice: str = Field(min_length=1, max_length=200)
    style: str = Field(default="", max_length=300)
    instructions: str = Field(default="", max_length=1500)


async def generate_speech(task_id, payload, state, production, root, tenant_id, gateway_factory, progress):
    from app.services.media_gateway import SpeechGenerationRequest
    from app.services.video_concat import run_media_command
    from app.services.video_replica import model_for, save

    options = SpeechInput.model_validate(payload["speech"])
    if not state.get("speech_asset"):
        async with SessionLocal() as db:
            model, provider = await model_for(db, tenant_id, options.model_id, ModelType.TTS)
            gateway = gateway_factory(provider)
            request = SpeechGenerationRequest(
                model=model.model_id,
                text=options.text,
                voice=options.voice,
                style=options.style,
                instructions=options.instructions,
                capabilities=model.capabilities or {},
                idempotency_key=f"{task_id}-{state.get('speech_attempt', 0)}",
            )
        await progress(10, "使用当前模型平台生成配音素材")
        if not state.get("speech_raw_key"):
            job = state.get("speech_provider_job_id")
            result = await gateway.poll_speech(request, job) if job else await gateway.submit_speech(request)
            async with asyncio.timeout(900):
                while True:
                    if result.provider_job_id:
                        state["speech_provider_job_id"] = result.provider_job_id
                        await save(task_id, state)
                    if result.status != "pending":
                        break
                    if not state.get("speech_provider_job_id"):
                        raise RuntimeError("配音平台未返回可恢复任务ID")
                    await progress(40, "配音正在合成，已保存平台任务ID")
                    await asyncio.sleep(5)
                    result = await gateway.poll_speech(request, state["speech_provider_job_id"])
            if result.status == "failed" or not result.audio_data:
                state.pop("speech_provider_job_id", None)
                state["speech_attempt"] = int(state.get("speech_attempt", 0)) + 1
                await save(task_id, state)
                raise RuntimeError(result.error_message or "配音平台返回空音频")
            raw = root / "voice-raw.audio"
            raw.write_bytes(result.audio_data)
            state["speech_raw_key"], _ = await persist_media_file(raw, result.content_type or "audio/mpeg")
            await save(task_id, state)
        raw = await materialize_media_file(state["speech_raw_key"])
        target = root / f"voice-{task_id}.wav"
        await run_media_command(
            "ffmpeg", "-v", "error", "-y", "-i", str(raw), "-vn", "-c:a", "pcm_s16le", str(target)
        )
        info = await hypit_bridge.command("media", "probe", str(target), "--json", workspace=root)
        key, url = await persist_media_file(target, "audio/wav")
        state["speech_asset"] = {
            "key": key,
            "url": url,
            "type": "audio",
            "name": "AI 配音",
            "duration": info["duration"],
            "text": options.text,
            "voice": options.voice,
        }
        await save(task_id, state)
    production["assets"] = {**production["assets"], f"assets/voice-{task_id}.wav": state["speech_asset"]}
    production["revision"] = revision({"files": production["files"], "assets": production["assets"]})
    state.update(production=production, media_url=state["speech_asset"]["url"])
    await save(task_id, state, finished=True)


def validate_sources(files, assets):
    if set(files) - set(SOURCE_FILES + DOCUMENT_FILES) or not set(SOURCE_FILES) <= set(files):
        raise ValueError("工程仅接受当前制作源文件和五份制作文档")
    if sum(len(value) for value in files.values()) > 240000:
        raise ValueError("制作工程超过240000字符")
    for name in SOURCE_FILES:
        source = files[name]
        if "<!DOCTYPE" in source.upper() or "<!ENTITY" in source.upper():
            raise ValueError("制作源文件不能声明外部实体")
        # Hypit parses its own declarative grammar; no user JS/TS modules are loaded.
        for attr, value in re.findall(r"\b(from|source|src)\s*=\s*[\"\']([^\"\']+)[\"\']", source):
            if attr == "from":
                match = re.fullmatch(r"@hypit/([a-z-]+)@1", value)
                if not match or match[1] not in PACKAGES:
                    raise ValueError("此工程只可导入已接入的本地 Hypit 制作组件")
            elif attr == "src":
                if value.removeprefix("./") not in assets:
                    raise ValueError("媒体来源必须是本工程已授权的素材")
            elif value.removeprefix("./") not in SOURCE_FILES:
                raise ValueError("源文件引用不能离开当前工程")
        # No expression-valued import/path attributes or alternate syntax to bypass path checks.
        if re.search(r"\b(?:from|src)\s*=\s*[^\s\"\']", source):
            raise ValueError("组件与媒体路径必须是明确的本地引用")
        if re.search(r"(?is)<import\b[^>]*\bsource\s*=\s*[^\s\"\']", source):
            raise ValueError("导入路径必须是明确的本地引用")
    if not re.search(r"<target\b[^>]*output=[\"\']final\.video[\"\']", files["render.svrun"]):
        raise ValueError("保留 final.video 作为当前工程的导出目标")


async def materialize(production, root):
    root.mkdir(parents=True, exist_ok=True)
    validate_sources(production["files"], production["assets"])
    for name, asset in production["assets"].items():
        # Manifest names are generated by the backend, never accepted in edit bodies.
        if not re.fullmatch(r"assets/[a-zA-Z0-9_-]+\.(mp4|webp|png|jpg|wav|mp3|ttf|otf)", name):
            raise ValueError("工程素材路径无效")
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            path = await materialize_media_file(asset["key"])
            shutil.copyfile(path, target)
    for name, content in production["files"].items():
        (root / name).write_text(content, encoding="utf-8")
    # No credentials or generation endpoints are placed in an editable/downloadable project.
    (root / "package.json").write_text('{"private":true,"type":"module"}', encoding="utf-8")
    profile = {
        "format": "hypit.runtime-local@1",
        "dataRoot": ".hypit/runtime-data",
        "credentials": {},
        "bindings": {},
        "endpoints": {
            "media.local": {"use": "@hypit/provider-media-local"},
            "hyperframes.local": {
                "use": "@hypit/provider-hyperframes-local",
                "config": {"workers": 1, "defaultConcurrency": 1, "browserCapacity": 1},
            },
        },
    }
    if "@hypit/whisperx@1" in production["files"]["composition.svml"]:
        from app.services.replica_transcription import PROFILE, STATE_HOME

        endpoint = json.loads(PROFILE.read_text(encoding="utf-8"))["endpoints"]["whisperx.local"]
        endpoint["config"]["modelCacheDirectory"] = str(STATE_HOME / "models")
        profile["endpoints"]["whisperx.local"] = endpoint
        profile["bindings"]["@hypit/whisperx@1#whisperx-alignment"] = "whisperx.local"
    (root / "hypit.runtime.json").write_text(json.dumps(profile, indent=2), encoding="utf-8")


async def prepare_speech(root, pulse=None):
    source = root / "composition.svml"
    if not source.exists() or "@hypit/whisperx@1" not in source.read_text(encoding="utf-8"):
        return
    from app.services.replica_transcription import PROFILE, STATE_HOME

    report = await hypit_bridge.command(
        "programs",
        "up",
        "--runtime",
        str(PROFILE),
        "--endpoint",
        "whisperx.local",
        "--json",
        workspace=root,
        timeout=1800,
        progress=pulse,
        env_overrides={"HYPIT_STATE_HOME": str(STATE_HOME)},
    )
    if not report.get("ready"):
        raise RuntimeError("本地语义字幕对齐服务未就绪，请重试准备语音服务")


async def check(production, root):
    await materialize(production, root)
    await prepare_fonts(root)
    return await hypit_bridge.command("check", "render.svrun", "--json", workspace=root, timeout=90)


async def prepare_fonts(root):
    """Resolve only pinned fonts shipped in Hypit's catalog, never arbitrary packages."""
    source = (root / "composition.svml").read_text(encoding="utf-8")
    if "@hypit/fonts-open@1" not in source:
        return
    catalog = hypit_bridge.INSTALL / "node_modules/@hypit/hypit/packages/fonts-open/package.json"
    dependencies = json.loads(catalog.read_text(encoding="utf-8")).get("optionalDependencies", {})
    for family in sorted(set(re.findall(r'\bfamily=["\']([a-z0-9-]+)["\']', source))):
        package = next(
            (p for p in dependencies if p in {f"@fontsource/{family}", f"@fontsource-variable/{family}"}),
            None,
        )
        if package:
            await hypit_bridge.command(
                "packages",
                "install",
                f"{package}@{dependencies[package]}",
                "--json",
                workspace=root,
                timeout=600,
            )


async def vocabulary(names):
    selected = [name for name in dict.fromkeys(names) if name in PACKAGES]
    if not selected or len(selected) > 4:
        raise ValueError("每次选择1到4个本地组件查询")
    result = await hypit_bridge.command(
        "vocabulary", *[f"@hypit/{n}" for n in selected], "--json", workspace=hypit_bridge.INSTALL, timeout=30
    )
    docs = {}
    for name in selected:
        path = hypit_bridge.INSTALL / "node_modules/@hypit/hypit/packages" / name / "README.md"
        if path.exists():
            docs[name] = path.read_text(encoding="utf-8")
    # Strip manifest bookkeeping, retaining the author grammar, types and examples.
    surfaces = []
    recipes = {}

    def compact(value):
        if isinstance(value, list):
            return [compact(item) for item in value]
        if isinstance(value, dict):
            if set(value) == {"module", "name"}:
                return value["module"]["name"] + "#" + value["name"]
            return {key: compact(item) for key, item in value.items() if key not in {"summary", "range"}}
        return value

    for surface in result.get("surfaces", []):
        vocab = surface.get("vocabulary", {})
        attributes = []
        for attr in vocab.get("attributes", []):
            entry = compact(
                {
                    key: value
                    for key, value in attr.items()
                    if key in {"name", "kind", "required", "values", "fallback", "recipe", "accepts"}
                }
            )
            if "recipe" in entry:
                recipe = entry.pop("recipe")
                key = next(
                    (key for key, value in recipes.items() if value == recipe), f"recipe-{len(recipes)}"
                )
                recipes[key] = recipe
                entry["recipe_ref"] = key
            attributes.append(entry)
        surfaces.append(
            {
                "package": surface["package"],
                "tag": surface["tag"],
                "outputs": compact(surface.get("outputs", [])),
                "attributes": attributes,
                "children": compact(vocab.get("children", [])),
                "ports": compact(vocab.get("ports", [])),
                "example": vocab.get("example"),
            }
        )
    return {"vocabulary": surfaces, "recipes": recipes, "documents": docs}


async def execute(
    task_id,
    payload,
    state,
    root,
    tenant_id,
    user_id,
    runtime_factory,
    progress,
    *,
    edit,
    speech=False,
    gateway_factory=None,
):
    from app.services.video_replica import finish_production, model_for, save

    production = dict(state.get("production") or payload["production"])
    production["files"] = dict(production["files"])
    if speech:
        await generate_speech(task_id, payload, state, production, root, tenant_id, gateway_factory, progress)
        return
    if edit and not state.get("edit_validated"):
        from app.services.replica_reading import ask

        async with SessionLocal() as db:
            model, provider = await model_for(db, tenant_id, payload["text_model_id"], ModelType.TEXT)
            caps = model.capabilities or {}
            binding = {
                "provider": caps.get("agentscope_provider") or provider.code,
                "model": model.model_id,
                "base_url": provider.base_url,
                "api_key": SecretBox().decrypt(provider.encrypted_api_key),
                "extra_headers": provider.extra_headers or {},
                "api_mode": caps.get("agent_api_mode") or "chat_completions",
                "max_tokens": 10000,
            }
        await progress(10, "按本次修改意图读取 Hypit 组件说明，不加载整套技能")
        references = await vocabulary(payload.get("components") or ["media-track"])
        if "caption-fine" in payload.get("components", []):
            from app.services import replica_transcription

            transcripts = dict(state.get("transcripts", {}))
            for name, asset in production["assets"].items():
                if asset.get("type") != "video" or not name.startswith("assets/clip-") or name in transcripts:
                    continue
                source = await materialize_media_file(asset["key"])
                info = await hypit_bridge.command("media", "probe", str(source), "--json", workspace=root)
                if not info.get("hasAudio"):
                    transcripts[name] = "无音轨，不制作对白字幕。"
                else:
                    transcript = await replica_transcription.transcribe(
                        source, root, payload.get("language", "zh"), lambda message: progress(20, message)
                    )
                    transcripts[name] = replica_transcription.context(transcript, 0, info["duration"])
                state["transcripts"] = dict(transcripts)
                await save(task_id, state)
            production["files"]["TRANSCRIPT.md"] = "\n\n".join(
                f"## {name}\n{text}" for name, text in transcripts.items()
            )
        current = production["files"]
        # Bound model input; fail before billing rather than truncate author source.
        prompt = (
            "在同一 Hypit 工程修改用户指定的内容。只返回需要变更文件的完整内容，其他文件不要返回。"
            "保持素材路径、既有行为和final.video出口；不能声明新素材或执行代码。"
            "只用提供的已安装组件语法，不要猜测属性。使用声明式SVML/SVS，不要Markdown代码围栏。"
            "声明必须先于引用：Normalize在SemanticTake之前，SemanticTake在Timeline之前，再声明画面/字幕Track。"
            "字幕、B-roll、声音与文字通过独立Track合成；修改编排不能重新请求视频生成。"
            "不改用户未要求的画面或人物。\n修改要求："
            + payload["instruction"]
            + "\n字幕只能使用本工程实际成片转写，不能根据原片猜测。"
            "对白通过script Segment与whisperx:SemanticTake绑定对应的media-i.media，"
            "保留原Timeline的end与各素材at位置；不能用语音末字时刻缩短视频总时长。"
            "再以time:Take放进现有time:Timeline，字幕共用该timeline；禁止用匀速分字伪造语音时间。"
            "无识别结果时不编造对白，可按用户要求添加普通标题。中文使用noto-sans-sc字体，显式language=zh/en。"
            + "\n本次组件文档："
            + json.dumps(references, ensure_ascii=False)
            + "\n工程源文件："
            + json.dumps({n: current[n] for n in SOURCE_FILES}, ensure_ascii=False)
            + "\n可用素材："
            + json.dumps(
                {n: {k: v for k, v in a.items() if k != "key"} for n, a in production["assets"].items()},
                ensure_ascii=False,
            )
            + "\n制作资料："
            + current.get("TRANSCRIPT.md", "")[:12000]
        )
        if len(prompt) > 65000:
            raise ValueError("本次工程上下文过大，请减少组件选择或直接编辑单个源文件")
        correction = ""
        for attempt in range(3):
            changed = await ask(
                runtime_factory,
                binding,
                tenant_id,
                user_id,
                task_id,
                f"edit-{attempt}",
                prompt + correction,
                SourceEdit,
                [],
                lambda: progress(35, "正在修改当前制作工程，保留已生成素材"),
            )
            candidate = {**production, "files": {**current, **changed.files}}
            try:
                validate_sources(candidate["files"], candidate["assets"])
                with tempfile.TemporaryDirectory(dir=root, prefix="check-") as directory:
                    await check(candidate, Path(directory))
                production = candidate
                break
            except (ValueError, RuntimeError) as exc:
                if attempt == 2:
                    raise
                correction = "\n上次修改没有通过原生Hypit校验，修复该错误：" + str(exc)[-2200:]
        production["revision"] = revision(production["files"])
        state["production"] = production
        state["edit_validated"] = True
        await save(task_id, state)
    await progress(80 if edit else 10, "校验本地制作工程")
    await check(production, root)
    state["production"] = production
    if edit:
        # User reviews the edited Source before explicitly exporting it.
        await save(task_id, state, finished=True)
    else:
        await finish_production(task_id, state, root, progress, payload.get("plan", {}))
