"""One explicitly requested paid repair probe, without saving task/content changes."""
import argparse
import asyncio
import json
import time
from types import SimpleNamespace

from openai import AsyncOpenAI
from app.db.models import AITask
from app.db.session import SessionLocal, engine
from app.services import task_worker as worker, storyboard_assets, storyboard_generation as generation


async def main(args):
    async with SessionLocal() as session:
        task = await session.get(AITask, args.task_id)
        worker.WORKER_ID = task.worker_id
        if not worker.owns_running_task(task):
            raise ValueError("Probe requires a running repair task")
    captured = {}
    class Captured(BaseException):
        pass
    async def intercept(*pos, **kwargs):
        captured.update(kwargs, prompt_code=pos[1], prompt=pos[2])
        raise Captured()
    original = storyboard_assets.storyboard_response
    storyboard_assets.storyboard_response = intercept
    try:
        await worker.execute_director_storyboard_repair_task(task.id, worker.default_runtime_factory)
    except Captured:
        pass
    finally:
        storyboard_assets.storyboard_response = original
    request = await worker.runtime_request(task.id, prompt_code=captured["prompt_code"], prompt=captured["prompt"])
    original_patch = generation.direct_patch_request
    def capture(req, instructions, candidate, neighbors):
        if candidate["order_index"] == args.shot:
            captured["direct"] = original_patch(req, instructions, candidate, neighbors)
            raise Captured()
        return req.model_copy(update={"prompt": json.dumps({"fields": {"image_prompt": candidate.get("image_prompt", "")}})})
    class Runtime:
        async def run(self, req):
            return SimpleNamespace(final_response=req.prompt, manifest={})
    async def noop(*args):
        pass
    generation.direct_patch_request = capture
    try:
        await generation.generate(request, Runtime, plan=captured["timing_plan"] or [],
            durations=captured["durations"], state={}, save=noop, progress=noop,
            script=captured.get("script", ""), budget=captured.get("budget"),
            repair_shots=captured["repair_shots"], findings=[item for item in captured["findings"]
                if args.shot in item.get("shot_indices", [])], repair_blocking_only=True)
    except Captured:
        pass
    finally:
        generation.direct_patch_request = original_patch
    direct = captured["direct"]
    binding = direct.model_binding
    started = time.monotonic()
    async with AsyncOpenAI(api_key=binding["api_key"], base_url=binding["base_url"], timeout=180, max_retries=0) as client:
        stream = await client.responses.create(model=binding["model"], stream=True,
            reasoning={"effort": args.effort}, input=[{"role": "system", "content": direct.system_prompt},
                                                     {"role": "user", "content": direct.prompt}])
        async with stream:
            async for event in stream:
                if event.type in {"response.completed", "response.incomplete", "response.failed"}:
                    output = "".join(getattr(part, "text", "") or "" for item in event.response.output
                        if item.type == "message" for part in (getattr(item, "content", []) or [])
                        if part.type == "output_text")
                    print(json.dumps({"seconds": round(time.monotonic()-started, 1),
                        "status": event.response.status, "output_chars": len(output),
                        "output_types": [item.type for item in event.response.output],
                        "patch_keys": [list(row) for row in generation.decode_repair_reply(output)] if output else []}), flush=True)
    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_id")
    parser.add_argument("--shot", type=int, required=True)
    parser.add_argument("--effort", choices=["none", "low", "medium", "high"], default="none")
    asyncio.run(main(parser.parse_args()))
