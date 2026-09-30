"""Review one existing shot against bounded source context; never saves a verdict."""
import argparse
import asyncio
import json
import time
from uuid import uuid4

from app.db.models import AITask, StoryboardVersion, ScriptVersion
from app.db.session import SessionLocal, engine
from app.services import task_worker as worker
from app.services.storyboard_review import review_units, direct_review_request, parse_review


async def main(args):
    async with SessionLocal() as session:
        task = await session.get(AITask, args.task)
        if task is None:
            raise ValueError("Task not found")
        board = await session.get(StoryboardVersion, task.request_payload["storyboard_version_id"])
        script = await session.get(ScriptVersion, board.script_version_id)
        rows = [{**row, "order_index": i} for i, row in enumerate(board.content, 1)]
        worker.WORKER_ID = task.worker_id
    request = await worker.runtime_request(task.id, prompt_code="storyboard-review",
        prompt="独立核验本镜与原文实际节拍。特别核对相邻候选源段，不以文字相似度确定场次。返回审核JSON。")
    request = request.model_copy(update={"project_files": [f for f in request.project_files
        if not f.id.startswith(("retrieval-chapter-shots", "retrieval-chapter-script", "retrieval-chapter-source"))]})
    unit = next(u for u in review_units(rows, script.content, max_shots=1)
        if not u["coverage_only"] and any(r["order_index"] == args.shot for r in u["shots"]))
    direct = direct_review_request(request, unit, [])
    if direct is None:
        raise ValueError("Evidence does not fit one direct request")
    print(json.dumps({"shot": args.shot, "candidate": unit.get("source_candidate_key"),
        "source_keys": [s["key"] for s in unit.get("source_context", [])],
        "input_chars": len(direct.prompt) + len(direct.system_prompt)}, ensure_ascii=False), flush=True)
    identity = "source-context-probe-" + uuid4().hex
    direct = direct.model_copy(update={"task_id": identity, "session_id": identity})
    start = time.monotonic()
    response = await asyncio.wait_for(worker.default_runtime_factory().run(direct), 180)
    verdict = parse_review(response.final_response, {args.shot}, len(rows),
        evidence_indices={r["order_index"] for r in unit["neighbors"] + unit["asset_history"]})
    print(json.dumps({"seconds": round(time.monotonic() - start, 1), "verdict": verdict}, ensure_ascii=False))
    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("task")
    parser.add_argument("shot", type=int)
    asyncio.run(main(parser.parse_args()))
