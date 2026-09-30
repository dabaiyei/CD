"""Local, serialized WhisperX transcription and bounded temporal context."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services import hypit_bridge

PROFILE = hypit_bridge.INSTALL / "transcription.runtime.json"
STATE_HOME = hypit_bridge.PROJECT_ROOT / "runtime-data" / "hypit-speech"


class Word(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    text: str = Field(max_length=2000)
    start_seconds: float | None = Field(default=None, ge=0)
    end_seconds: float | None = Field(default=None, ge=0)
    score: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def ordered(self):
        if (
            self.start_seconds is not None
            and self.end_seconds is not None
            and self.end_seconds < self.start_seconds
        ):
            raise ValueError("转写时间戳顺序错误")
        return self


class Passage(Word):
    text: str = Field(max_length=30000)
    words: list[Word] = Field(default_factory=list, max_length=10000)


class Transcript(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    language: str
    audio_seconds: float = Field(ge=0, le=181)
    passages: list[Passage] = Field(max_length=10000)


def normalize(raw: dict) -> dict:
    if raw.get("format") != "hypit.transcript@1":
        raise ValueError("转写结果格式无效")
    result = Transcript.model_validate(raw).model_dump(exclude_none=True)
    for passage in result["passages"]:
        passage["text"] = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", passage["text"])
        for item in [passage, *passage["words"]]:
            if item.get("end_seconds", 0) > result["audio_seconds"] + 1:
                raise ValueError("转写时间戳超出音轨长度")
    return {"format": "hypit.transcript@1", **result}


def context(transcript: dict | None, start: float, end: float) -> str:
    if not transcript:
        return "没有音频转写，不得编造对白。"
    rows = []
    for passage in transcript.get("passages", []):
        timed = [w for w in passage.get("words", []) if "start_seconds" in w and "end_seconds" in w]
        if timed:
            selected = [w for w in timed if w["start_seconds"] < end and w["end_seconds"] > start]
            if selected:
                text = " ".join(w["text"] for w in selected)
                text = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", text)
                rows.append(f"{selected[0]['start_seconds']:.2f}—{selected[-1]['end_seconds']:.2f}秒：{text}")
            # Untimed words are not silently dropped or assigned invented timestamps.
            untimed = [
                w["text"]
                for w in passage.get("words", [])
                if "start_seconds" not in w or "end_seconds" not in w
            ]
            if selected and untimed:
                rows.append("同段未对齐词（不可确定具体时刻）：" + " ".join(untimed))
        elif "start_seconds" in passage and "end_seconds" in passage:
            if passage["start_seconds"] < end and passage["end_seconds"] > start:
                rows.append(
                    f"{passage['start_seconds']:.2f}—{passage['end_seconds']:.2f}秒（整段）：{passage['text']}"
                )
        elif passage["text"].strip():
            rows.append("未对齐台词（不可认定发生在本段）：" + passage["text"])
    text = "\n".join(rows)
    if len(text) > 6000:
        text = text[:6000] + "\n[本段转写上下文已截断，不得补写缺失台词]"
    return text or "本段未识别到对白，不得编造对白。"


@asynccontextmanager
async def service_slot(progress):
    # OS-owned locks release on worker crashes; all processes on this host share one service.
    STATE_HOME.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(STATE_HOME / "whisperx.lock", os.O_RDWR | os.O_CREAT, 0o600), "r+b") as handle:
        async with asyncio.timeout(1800):
            while True:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    await progress("等待本地语音转写服务，已保留任务进度")
                    await asyncio.sleep(5)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


async def transcribe(source: Path, root: Path, language: str, progress) -> dict:
    if language not in {"zh", "en"}:
        raise ValueError("当前本地转写支持中文或英文")
    async with service_slot(progress):
        await progress("正在准备本地 WhisperX 语音转写服务")
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
            env_overrides={"HYPIT_STATE_HOME": str(STATE_HOME)},
            progress=lambda: progress("正在准备本地语音模型，首次使用需要下载，请稍候"),
        )
        if not report.get("ready"):
            raise RuntimeError("本地转写服务未就绪，请检查 Hypit programs 日志后重试")
        await progress("正在识别原片对白并对齐时间戳")
        with tempfile.TemporaryDirectory(dir=root, prefix="transcript-") as directory:
            target = Path(directory) / "transcript.json"
            await hypit_bridge.command(
                "transcribe",
                str(source),
                "--to",
                str(target),
                "--language",
                language,
                "--runtime",
                str(PROFILE),
                "--json",
                workspace=root,
                timeout=600,
                env_overrides={"HYPIT_STATE_HOME": str(STATE_HOME)},
                progress=lambda: progress("正在本地识别对白并对齐时间戳"),
            )
            return normalize(json.loads(target.read_text(encoding="utf-8")))
