import asyncio
import json
from pathlib import Path

from PIL import Image

from app.services import hypit_bridge
from app.services.agent_runtime import AgentRuntimeResponse
from app.services.task_worker import process_task


def test_whole_reading_adaptive_inspection_and_resume(client, admin_headers, monkeypatch):
    monkeypatch.setattr(hypit_bridge, "available", lambda: True)
    samples = []

    async def command(*args, **kwargs):
        if args[:2] == ("media", "probe"):
            return {"duration": 24, "hasVideo": True, "hasAudio": False, "width": 320, "height": 180}
        moments = list(map(float, args[args.index("--at") + 1].split(",")))
        samples.append(moments)
        folder = Path(args[args.index("--to") + 1])
        folder.mkdir()
        result = []
        for i, at in enumerate(moments):
            path = folder / f"{i}.png"
            Image.new("RGB", (32, 18), "red").save(path)
            result.append({"at": at, "path": str(path)})
        return {"frames": result}

    monkeypatch.setattr(hypit_bridge, "command", command)
    config = client.get("/api/v1/video-replicas/config", headers=admin_headers).json()
    text = next(m["id"] for m in config["models"] if m["type"] == "text")
    result = client.post(
        "/api/v1/video-replicas",
        headers=admin_headers,
        data={"brief": "替换主角，保留事件关系", "text_model_id": text},
        files={"file": ("test.mp4", b"fixture", "video/mp4")},
    )
    assert result.status_code == 202, result.text
    task_id = result.json()["id"]
    assert result.json()["request_payload"]["analysis_version"] == 3
    calls = []
    fail = True

    class Runtime:
        async def run(self, request):
            label = request.session_id.removeprefix(task_id + "-")
            calls.append(label)
            assert len(request.attachments) <= 4
            assert request.tool_mode == "none" and not request.skills
            if label.startswith(("whole", "inspect")):
                data = {
                    "analysis": "两人相遇后追逐，字幕补充信息",
                    "treatment": "保持事件顺序，用用户角色替换主角",
                    "passages": [
                        {"start": 0, "end": 7, "focus": "相遇", "frames": 8},
                        {"start": 7, "end": 24, "focus": "追逐", "frames": 32},
                    ],
                    "inspect": [{"start": 5, "end": 7, "focus": "确认起跑动作"}]
                    if label.startswith("whole")
                    else [],
                }
            else:
                index = int(label.split("-")[1])
                if fail and index == 1:
                    raise RuntimeError("第二语义段临时不可用")
                start, end = (0, 7) if index == 0 else (7, 24)
                data = {
                    "summary": "本段",
                    "shots": [
                        {
                            "start": start,
                            "end": end,
                            "observation": "参考动作",
                            "prompt": "保持本段动作，替换主角",
                            "scene_id": "street",
                        }
                    ],
                }
            return AgentRuntimeResponse(
                session_id=request.session_id,
                final_response=json.dumps(data),
                finish_reason="completed",
                events=[],
                manifest={},
            )

    asyncio.run(process_task(task_id, runtime_factory=Runtime))
    failed = client.get(f"/api/v1/tasks/{task_id}", headers=admin_headers).json()
    assert failed["status"] == "failed", failed
    assert failed["result_payload"]["reading_inspected"]
    assert list(failed["result_payload"]["batches"]) == ["0"]
    assert failed["result_payload"]["reading_ranges"][0]["end"] == 7
    assert any(len(s) == 12 and all(5 <= t <= 7 for t in s) for s in samples)
    calls.clear()
    fail = False
    assert client.post(f"/api/v1/tasks/{task_id}/retry", headers=admin_headers).status_code == 202
    asyncio.run(process_task(task_id, runtime_factory=Runtime))
    done = client.get(f"/api/v1/tasks/{task_id}", headers=admin_headers).json()
    assert done["status"] == "succeeded", done
    assert calls == ["passage-1-0-0"]
    assert done["result_payload"]["plan"]["shots"][-1]["end"] == 24
    assert set(done["result_payload"]["production_documents"]) == {
        "BRIEF.md",
        "ANALYSIS.md",
        "TREATMENT.md",
        "TIMELINE.md",
        "PROGRESS.md",
    }
