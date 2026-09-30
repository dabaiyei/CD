"""Paid, read-only generation probe from a saved batch workspace; no task replay."""
import asyncio
import json
import sys
import time
from pathlib import Path
from uuid import uuid4

from app.core.config import get_settings
from app.core.security import SecretBox
from app.db.models import AIModel, AITask, Provider
from app.db.session import SessionLocal, engine
from app.services.agent_runtime import AgentRuntimeClient, AgentRuntimeRequest
from app.services.retrieval_context import evidence_file
from app.services.storyboard_generation import decode_shots
from app.services.storyboard_preparation import prepare, direct_request, source_units, coverage, mapping_request, merge_mapping
from app.services.storyboard_review import run_review_attempt
from app.services.task_worker import GeneratedStoryboardShotPayload


async def main(task_id, workspace):
    root = Path(workspace) / 'project-files'
    local = (root / 'retrieval-local-operation/local-operation.md').read_text(encoding='utf-8')
    async with SessionLocal() as db:
        task = await db.get(AITask, task_id)
        # A model ID can be supplied when the original task was deleted. The
        # source is still the saved local batch, never a fabricated chapter.
        model = await db.get(AIModel, task.model_id if task else task_id)
        if model is None:
            raise ValueError('Task/model no longer exists; pass an existing model ID')
        provider = await db.get(Provider, model.provider_id)
        if task:
            segments = task.request_payload['storyboard_generation']['state']['segments']
            segment = next(s for s in segments if s['content'] in local)
        else:
            segment = {'key': 'probe', 'content': local.split('本场正文：\n', 1)[1].split('\n跨批次衔接契约', 1)[0]}
        caps = model.capabilities or {}
        probe_id = str(uuid4())
        req = AgentRuntimeRequest(tenant_id=task.tenant_id if task else model.tenant_id,
            project_id=task.project_id if task else probe_id,
            task_id=probe_id, session_id=probe_id, prompt='', system_prompt='',
            prompt_versions={}, skill_versions={}, skills=[], memory_context=[],
            model_binding={'provider': provider.code, 'model': model.model_id,
                'base_url': provider.base_url, 'api_key': SecretBox().decrypt(provider.encrypted_api_key),
                'extra_headers': provider.extra_headers or {},
                'api_mode': caps.get('agent_api_mode', 'chat_completions'),
                'reasoning_effort': 'high', 'max_tokens': caps.get('max_tokens')})
    files = []
    for name in ('task-rules', 'task-references', 'task-memory'):
        path = root / f'retrieval-{name}' / f'{name}.md'
        if path.exists():
            files.append(evidence_file(name, path.read_text(encoding='utf-8')))
    req = req.model_copy(update={'project_files': files})
    spans = source_units(segment['key'], segment['content'])
    prompt = local + ('\n本次分页最多返回3个完整片段，按顺序覆盖原文，不得跳段或压缩台词。'
        '每个shots项额外提供source_ids数组，引用实际呈现的下列原文编号，无法容纳的留给下一页。'
        '\n待覆盖原文：' + json.dumps(spans, ensure_ascii=False))
    req = direct_request(req, prompt, prepare(req, {}), segment['content'])
    settings = get_settings()
    client = AgentRuntimeClient(settings.agent_runtime_url, settings.agent_runtime_internal_token)

    async def progress(message):
        print(message, flush=True)

    started = time.monotonic()
    result = await run_review_attempt(client, req, progress, '正在生成分镜独立探针')
    rows = decode_shots(result.final_response)
    assert rows, 'No complete shots returned'
    print(json.dumps({'returned_source_ids': [r.get('source_ids') for r in rows],
                      'expected_ids': [s['id'] for s in spans]}, ensure_ascii=False))
    for row in rows:
        GeneratedStoryboardShotPayload.model_validate(row)
    mapping_calls = 0
    try:
        missing = coverage(rows, spans)
    except ValueError:
        mapping = await run_review_attempt(client, mapping_request(req, rows, spans), progress, '正在修复探针原文关联')
        rows = merge_mapping(rows, spans, mapping.final_response)
        missing = coverage(rows, spans)
        mapping_calls = 1
    print(json.dumps({'elapsed_seconds': round(time.monotonic() - started, 1),
        'shots': len(rows), 'source_spans': len(spans), 'covered': len(spans) - len(missing),
        'mapping_calls': mapping_calls,
        'finish_reason': result.finish_reason, 'input_chars': len(req.prompt) + len(req.system_prompt),
        'model_calls': sum(e.get('type') == 'MODEL_CALL_START' for e in result.events),
        'tool_calls': sum(e.get('type') == 'TOOL_CALL_START' for e in result.events)}, ensure_ascii=False))
    await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main(*sys.argv[1:]))
