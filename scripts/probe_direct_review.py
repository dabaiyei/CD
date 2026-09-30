"""Read-only single-shot diagnostic using a running task's real review context.

Pass --run to make one paid model call; never saves a verdict or changes tasks.
Credentials and source text are never printed.
"""
import argparse
import asyncio
import json
import time
from uuid import uuid4

from app.db.models import AITask, ScriptVersion, Project
from app.db.session import SessionLocal, engine
from app.services import task_worker as worker
from app.services.storyboard_review import direct_review_request, direct_review_packets, review_units, parse_review


async def main(args):
    if args.capture:
        await capture_review(args)
        await engine.dispose()
        return
    async with SessionLocal() as session:
        task = await session.get(AITask, args.task_id)
        if task is None or str(task.status) != "running":
            raise ValueError("Diagnostic requires a currently running task")
        state = task.request_payload.get("storyboard_generation", {}).get("state", {})
        rows = [{**row, "order_index": int(key)} for key, row in state.get("valid", {}).items()]
        rows.sort(key=lambda row: row["order_index"])
        script = await session.get(ScriptVersion, task.request_payload.get("script_version_id"))
        project = await session.get(Project, task.project_id)
        video = await worker.project_video_model_for_storyboard(session, project=project, tenant_id=task.tenant_id)
        contract = worker.video_model_execution_contract(video,
            requested_resolution=project.video_resolution or "", aspect_ratio=project.aspect_ratio or "")
        prompt = "独立审核当前单镜，返回 approved、summary、findings。模型执行契约：" + json.dumps(contract, ensure_ascii=False)
        # Only this diagnostic process adopts the task's read guard. It calls
        # context assembly, never a worker executor, checkpoint or commit.
        worker.WORKER_ID = task.worker_id
    request = await worker.runtime_request(task.id, prompt_code="storyboard-review", prompt=prompt)
    request = request.model_copy(update={"project_files": [file for file in request.project_files
        if not file.id.startswith(("retrieval-chapter-shots", "retrieval-chapter-script", "retrieval-chapter-source"))]})
    units = review_units(rows, script.content, max_shots=1)
    unit = units[0]
    direct = direct_review_request(request, unit, rows)
    eligible = sum(direct_review_request(request, entry, [row for row in rows
        if row["order_index"] in entry["scene_shot_indices"]]) is not None for entry in units)
    sizes = [len(candidate.prompt) + len(candidate.system_prompt) for entry in units
        if (candidate := direct_review_request(request, entry, [row for row in rows
            if row["order_index"] in entry["scene_shot_indices"]], budget=1000000))]
    largest = max(units, key=lambda entry: len(json.dumps(entry, ensure_ascii=False)))
    print(json.dumps({"evidence_fields": {key: len(json.dumps(value, ensure_ascii=False)) for key, value in largest.items()},
        "reference_sizes": {file.id: len(file.content) for file in request.project_files},
        "system_chars": len(request.system_prompt)}, ensure_ascii=False), flush=True)
    print(json.dumps({"units": len(units), "direct_eligible": eligible, "direct": direct is not None,
        "max_chars": max(sizes, default=0), "under_48000": sum(size <= 48000 for size in sizes),
        "chars": len(direct.prompt) + len(direct.system_prompt) if direct else None}, ensure_ascii=False), flush=True)
    if args.run and direct:
        identity = "review-probe-" + uuid4().hex
        direct = direct.model_copy(update={"task_id": identity, "session_id": identity})
        events = []
        async def record(event):
            events.append(event.get("type"))
        start = time.monotonic()
        response = await asyncio.wait_for(worker.default_runtime_factory().run_stream(direct, record), 180)
        verdict = parse_review(response.final_response, {row["order_index"] for row in unit["shots"]}, len(rows))
        print(json.dumps({"seconds": round(time.monotonic() - start, 1),
            "model_calls": events.count("MODEL_CALL_START"), "tool_calls": events.count("TOOL_CALL_START"),
            "approved": verdict["approved"], "findings": len(verdict["findings"])}, ensure_ascii=False), flush=True)
    await engine.dispose()


async def capture_review(args):
    """Capture the production executor's exact inputs without executing/saving."""
    from app.services import storyboard_review as review, generation_parallel as parallel
    async with SessionLocal() as session:
        task = await session.get(AITask, args.task_id)
        worker.WORKER_ID = task.worker_id
    class Captured(BaseException):
        pass
    captured = {}
    async def intercept(request, runtime_factory, **kwargs):
        captured.update(request=request, **kwargs)
        raise Captured()
    async def no_reservation(*args):
        return 4
    original_review, original_reserve = review.review_board, parallel.reserve_task_slots
    review.review_board, parallel.reserve_task_slots = intercept, no_reservation
    try:
        await worker.execute_director_storyboard_review_task(task.id, worker.default_runtime_factory)
    except Captured:
        pass
    finally:
        review.review_board, parallel.reserve_task_slots = original_review, original_reserve
    request = captured["request"]
    print(json.dumps({"model": {key: request.model_binding.get(key) for key in ("model", "api_mode", "reasoning_effort", "max_tokens")}}, ensure_ascii=False))
    request = request.model_copy(update={"project_files": [file for file in request.project_files
        if not file.id.startswith(("retrieval-chapter-shots", "retrieval-chapter-script", "retrieval-chapter-source"))]})
    rows = [{**row, "order_index": i} for i, row in enumerate(captured["shots"], 1)]
    catalog = next((file for file in request.project_files if file.id == "retrieval-project-assets"), None)
    if catalog:
        names = {row["id"]: row["name"] for row in json.loads(catalog.content)}
        rows = [{**row, "asset_names": row.get("asset_names") or [names[key] for key in row.get("asset_ids", []) if key in names]} for row in rows]
    units = review_units(rows, captured["script"], max_shots=1)
    print(json.dumps({"reference_sizes": {file.id: len(file.content) for file in request.project_files},
        "prompt_chars": len(request.prompt), "system_chars": len(request.system_prompt), "units": len(units)}, ensure_ascii=False))
    sizes = []
    for unit in units:
        req = direct_review_request(request, unit, [row for row in rows if row["order_index"] in unit["scene_shot_indices"]], budget=1000000)
        sizes.append(len(req.prompt) + len(req.system_prompt) if req else -1)
    print(json.dumps({"direct_eligible": sum(0 < size <= 40000 for size in sizes),
        "min_chars": min(sizes), "max_chars": max(sizes), "sizes": sizes}, ensure_ascii=False), flush=True)
    packets = [direct_review_packets(request, unit, [row for row in rows if row["order_index"] in unit["scene_shot_indices"]])
               for unit in units]
    print(json.dumps({"packet_counts": [len(group) for group in packets],
        "all_tool_free": all(packet.tool_mode == "none" for group in packets for packet in group),
        "max_packet_chars": max(len(packet.prompt) + len(packet.system_prompt) for group in packets for packet in group)}, ensure_ascii=False), flush=True)
    if args.raw:
        from openai import AsyncOpenAI
        from collections import Counter
        chosen = packets[args.unit - 1][0]
        binding = chosen.model_binding
        counts = Counter()
        text_chars = 0
        started = time.monotonic()
        async with AsyncOpenAI(api_key=binding["api_key"], base_url=binding["base_url"],
                               timeout=300, max_retries=0) as client:
            stream = await client.responses.create(model=binding["model"], stream=True,
                reasoning={"effort": args.effort or binding.get("reasoning_effort") or "high"},
                input=[{"role": "system", "content": chosen.system_prompt},
                       {"role": "user", "content": chosen.prompt}])
            async with stream:
                async for event in stream:
                    counts[event.type] += 1
                    if event.type == "response.output_text.delta":
                        text_chars += len(event.delta)
                    if event.type in {"response.completed", "response.incomplete", "response.failed"}:
                        output = [{"type": item.type, "text_lengths": [len(getattr(p, "text", "") or "")
                            for p in (getattr(item, "content", []) or [])]} for item in event.response.output]
                        print(json.dumps({"seconds": round(time.monotonic()-started, 1),
                            "types": dict(counts), "delta_text_chars": text_chars, "output": output,
                            "status": event.response.status,
                            "incomplete": str(event.response.incomplete_details)}, ensure_ascii=False), flush=True)
                        text_result = "".join(getattr(part, "text", "") or ""
                            for item in event.response.output if item.type == "message"
                            for part in (getattr(item, "content", []) or []) if part.type == "output_text")
                        if text_result:
                            verdict = parse_review(text_result, {row["order_index"] for row in units[args.unit - 1]["shots"]}, len(rows))
                            print(json.dumps({"approved": verdict["approved"], "findings": verdict["findings"]}, ensure_ascii=False), flush=True)
        return
    if args.run:
        chosen = packets[args.unit - 1][0]
        identity = "review-probe-" + uuid4().hex
        binding = dict(chosen.model_binding)
        if args.effort:
            binding["reasoning_effort"] = args.effort
        chosen = chosen.model_copy(update={"task_id": identity, "session_id": identity, "model_binding": binding})
        events = []
        async def record(event):
            events.append(event.get("type"))
        start = time.monotonic()
        result = await asyncio.wait_for(worker.default_runtime_factory().run_stream(chosen, record), 180)
        verdict = parse_review(result.final_response, {row["order_index"] for row in units[args.unit - 1]["shots"]}, len(rows))
        print(json.dumps({"seconds": round(time.monotonic() - start, 1), "model_calls": events.count("MODEL_CALL_START"),
            "tool_calls": events.count("TOOL_CALL_START"), "findings": verdict["findings"], "approved": verdict["approved"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("task_id")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--capture", action="store_true")
    parser.add_argument("--raw", action="store_true", help="One provider call; log event counts only")
    parser.add_argument("--unit", type=int, default=1)
    parser.add_argument("--effort", choices=["low", "medium", "high"])
    asyncio.run(main(parser.parse_args()))
