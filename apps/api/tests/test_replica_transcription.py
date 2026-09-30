import asyncio
import json
from pathlib import Path

import pytest

from app.services import replica_transcription as speech


def transcript():
    return {
        "format": "hypit.transcript@1",
        "language": "zh",
        "audio_seconds": 24,
        "passages": [
            {
                "text": "你 好",
                "start_seconds": 1,
                "end_seconds": 14,
                "words": [
                    {"text": "你", "start_seconds": 1, "end_seconds": 2},
                    {"text": "好", "start_seconds": 13, "end_seconds": 14},
                ],
            }
        ],
    }


def test_timed_words_sliced_without_repeating_entire_passage():
    result = speech.normalize(transcript())
    assert result["passages"][0]["text"] == "你好"
    assert "你" in speech.context(result, 0, 12)
    assert "好" not in speech.context(result, 0, 12)
    assert "好" in speech.context(result, 12, 24)
    assert "你" not in speech.context(result, 12, 24)


def test_invalid_and_untimed_results():
    raw = transcript()
    raw["passages"][0]["end_seconds"] = float("nan")
    with pytest.raises(ValueError):
        speech.normalize(raw)
    raw = transcript()
    raw["passages"][0]["words"][0]["end_seconds"] = 99
    with pytest.raises(ValueError):
        speech.normalize(raw)
    assert "未对齐" in speech.context({"passages": [{"text": "台词"}]}, 0, 12)
    assert "未识别到" in speech.context({"passages": []}, 0, 12)
    assert len(speech.context({"passages": [{"text": "词" * 10000}]}, 0, 12)) < 6100


def test_reads_transcript_file_not_cli_summary_and_releases_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(speech, "STATE_HOME", tmp_path / "state")
    calls = []

    async def command(*args, **kwargs):
        calls.append(args[0])
        if args[0] == "programs":
            return {"ready": True}
        Path(args[args.index("--to") + 1]).write_text(json.dumps(transcript()), encoding="utf-8")
        return {"passages": 1, "words": 2}

    monkeypatch.setattr(speech.hypit_bridge, "command", command)

    async def progress(message):
        pass

    for _ in range(2):
        result = asyncio.run(speech.transcribe(tmp_path / "source.mp4", tmp_path, "zh", progress))
        assert result["passages"][0]["text"] == "你好"
    assert calls == ["programs", "transcribe", "programs", "transcribe"]


def test_service_failure_does_not_return_empty_success(tmp_path, monkeypatch):
    monkeypatch.setattr(speech, "STATE_HOME", tmp_path / "state")

    async def command(*args, **kwargs):
        return {"ready": False}

    async def progress(message):
        pass

    monkeypatch.setattr(speech.hypit_bridge, "command", command)
    with pytest.raises(RuntimeError, match="未就绪"):
        asyncio.run(speech.transcribe(tmp_path / "source.mp4", tmp_path, "zh", progress))
