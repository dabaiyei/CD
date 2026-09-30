from types import SimpleNamespace
import asyncio
import json

import pytest

from app.api.routes.agent_chat import asset_regeneration_requested
from app.services.task_worker import director_chapter_instructions


@pytest.mark.parametrize("text,expected", [
    ("重新生成冬哥的资产图片", True), ("重新生图", True),
    ("只补缺失图片，不要重新生成", False), ("生成资产图片", False),
])
def test_asset_regeneration_scope(text, expected):
    assert asset_regeneration_requested(text) is expected


def test_project_chat_exposes_current_execution_contract_and_local_scope():
    text = director_chapter_instructions(SimpleNamespace(id="c", title="章节"))
    assert "execution-contract.json" in text
    assert "only_missing_image/only_missing_prompt必须为false" in text
    assert "不擅自启动全章全自动" in text
    assert "分批保存、局部字段修复和断点续作" in text


def test_execution_snapshot_uses_production_resolvers_and_keeps_chat_available(monkeypatch):
    from app.services import task_worker as worker
    calls = []
    async def image(*args, **kwargs):
        calls.append("image")
        raise RuntimeError("图片模型已停用")
    async def video(*args, **kwargs):
        calls.append("video")
        return None
    monkeypatch.setattr(worker, "project_image_model_for_storyboard", image)
    monkeypatch.setattr(worker, "project_video_model_for_storyboard", video)
    project = SimpleNamespace(aspect_ratio="16:9", image_resolution="2K", video_resolution="720p", first_frame_mode=False)
    snapshot = asyncio.run(worker.project_execution_snapshot(None, project, "tenant"))
    content = json.loads(snapshot.content)
    assert calls == ["image", "video"]
    assert content["image"]["unavailable"] == "图片模型已停用"
    assert content["video"]["unavailable"] == "未配置可用模型"
    assert content["aspect_ratio"] == "16:9"
    assert not snapshot.editable
