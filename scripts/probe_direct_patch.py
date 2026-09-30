"""Inspect actual repair input sizes without model calls or database writes."""
import asyncio
import json
import sys
from types import SimpleNamespace

from app.db.models import AITask
from app.db.session import SessionLocal, engine
from app.services import task_worker as worker, storyboard_assets, storyboard_generation as generation


async def main(task_id):
    async with SessionLocal() as session:
        task = await session.get(AITask, task_id)
        if task is None:
            raise ValueError("Repair task not found")
        worker.WORKER_ID = task.worker_id
        if not worker.owns_running_task(task):
            raise ValueError("Diagnostic requires a running repair task")
    captured = {}
    class Captured(BaseException):
        pass
    async def intercept(*args, **kwargs):
        captured.update(kwargs, prompt_code=args[1], prompt=args[2])
        raise Captured()
    original = storyboard_assets.storyboard_response
    storyboard_assets.storyboard_response = intercept
    try:
        await worker.execute_director_storyboard_repair_task(task_id, worker.default_runtime_factory)
    except Captured:
        pass
    finally:
        storyboard_assets.storyboard_response = original
    request = await worker.runtime_request(task_id, prompt_code=captured['prompt_code'], prompt=captured['prompt'])
    original_patch = generation.direct_patch_request
    def inspect(req, instructions, candidate, neighbors):
        direct = original_patch(req, instructions, candidate, neighbors)
        from app.services.storyboard_review import local_asset_evidence
        assets = local_asset_evidence(req, {'shots': [candidate], 'neighbors': [v for v in neighbors.values() if v], 'asset_history': []})
        print(json.dumps({'shot': candidate['order_index'], 'direct': direct is not None,
            'instructions': len(instructions), 'target': len(json.dumps(candidate, ensure_ascii=False)),
            'neighbors': len(json.dumps(neighbors, ensure_ascii=False)),
            'assets': len(json.dumps(assets, ensure_ascii=False)),
            'references': {f.id: len(f.content) for f in req.project_files if f.id.startswith(('retrieval-task-rules', 'retrieval-task-memory'))}}, ensure_ascii=False), flush=True)
        # The generator only simulates merging its original fields in memory.
        return req.model_copy(update={'prompt': json.dumps({'fields': {k: v for k, v in candidate.items() if k in generation.REPAIR_FIELDS}})})
    class Runtime:
        async def run(self, req):
            return SimpleNamespace(final_response=req.prompt, manifest={})
    async def noop(*args):
        pass
    generation.direct_patch_request = inspect
    try:
        await generation.generate(request, Runtime, plan=captured['timing_plan'] or [],
            durations=captured['durations'], state={}, save=noop, progress=noop,
            script=captured.get('script', ''), budget=captured.get('budget'),
            repair_shots=captured['repair_shots'], findings=captured['findings'],
            feedback=captured.get('feedback', ''), repair_blocking_only=True)
    finally:
        generation.direct_patch_request = original_patch
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main(sys.argv[1]))
