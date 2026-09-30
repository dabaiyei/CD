import asyncio
import json
from types import SimpleNamespace

import pytest

from app.services.retrieval_context import automatic_request
from app.services.storyboard_preparation import (INPUT_BUDGET, prepare, direct_request,
    coverage, output_capacity, compact_generation_input)
from app.services.storyboard_preparation import mapping_request, merge_mapping
from app.services.storyboard_generation import generate
from test_storyboard_generation import request, shot

pytestmark = pytest.mark.usefixtures('jev_disabled')


def test_preparation_cache_and_exact_requested_reference():
    req = automatic_request(request(), rules='时长8秒', references='# 光线\n背光\n# 动作\n连续动作')
    state = {}
    packet = prepare(req, state)
    assert prepare(req, state) is packet
    identifier = packet['sections'][1]['id']
    result = direct_request(req, '本批原文', packet, '动作', requested=[identifier])
    assert result.tool_mode == 'none'
    assert not result.project_files and not result.skills
    assert '连续动作' in result.prompt and '时长8秒' in result.system_prompt
    with pytest.raises(ValueError, match='不存在'):
        direct_request(req, '原文', packet, '', requested=['invented'])
    changed = req.model_copy(update={'prompt_versions': {'revision': '2'}})
    assert prepare(changed, state)['fingerprint'] != packet['fingerprint']


def test_coverage_and_output_capacity_are_strict():
    spans = [{'id': 'a'}, {'id': 'b'}]
    assert coverage([{'source_ids': ['a']}], spans) == {'b'}
    for invalid in ([], ['invented'], 'a', [None]):
        with pytest.raises(ValueError):
            coverage([{'source_ids': invalid}], spans)
    assert output_capacity({'max_tokens': 4000}) == 1
    assert output_capacity({}) == 3


def test_large_reference_index_cannot_crowd_out_source_and_rules():
    rules = '硬约束' * 5100
    source = '本批原文' * 6000
    sections = [{'id': str(i), 'title': f'手册{i}', 'text': '原始规则' * 300,
                 'kind': 'reference'} for i in range(121)]
    result = direct_request(request(), source, {'rules': rules, 'sections': sections}, '手册')
    assert result.system_prompt.startswith(rules)
    assert result.prompt.startswith(source)
    assert len(result.system_prompt) + len(result.prompt) <= INPUT_BUDGET
    assert '原始规则' in result.prompt


def test_impossible_input_is_distinguished_from_transient_model_failure():
    from app.services.storyboard_preparation import StoryboardInputBudgetError, budget_blocked
    with pytest.raises(StoryboardInputBudgetError) as error:
        direct_request(request(), '原文' * 101000, {'rules': '保留', 'sections': []}, '')
    assert budget_blocked(error.value)
    assert not budget_blocked('provider timeout')


def test_reported_repair_batch_size_is_accepted_without_source_deletion():
    source = '本批原文' * 9950
    rules = '硬约束' * 8071
    result = direct_request(request(), source, {'rules': rules, 'sections': []}, '')
    assert source in result.prompt
    assert rules in result.system_prompt
    assert len(result.system_prompt) + len(result.prompt) < INPUT_BUDGET


def test_source_windows_cover_every_span_once_in_order():
    from app.services.storyboard_preparation import source_units, source_window
    spans = source_units('1', ('甲推门进屋并观察周围。' * 600))
    missing = {s['id'] for s in spans}
    seen = []
    while missing:
        window = source_window(spans, missing)
        assert window and len(json.dumps(window, ensure_ascii=False)) <= 2400
        seen.extend(window)
        missing -= {s['id'] for s in window}
    assert seen == spans


def test_oversized_repair_input_keeps_source_and_shot_count_summary():
    rows = [{"order_index": i, "title": f"镜头{i}", "duration_seconds": 8,
             "action_description": "很长的已保存动作" * 500} for i in range(1, 15)]
    prompt = ('固定约束\n本场正文：\n' + '源文' * 5000
              + '\n原分镜中本场的 14 个镜头如下：\n' + json.dumps(rows, ensure_ascii=False)
              + '\n跨批次衔接契约\n保持人物站位')
    compact = compact_generation_input(prompt)
    assert len(compact) < len(prompt) / 4
    assert '源文源文' not in compact
    assert '镜头14' in compact and '保持人物站位' in compact


def test_input_budget_failure_does_not_repeat_local_attempts():
    req = automatic_request(request(), rules='硬约束' * 70000)
    calls = []
    async def save(state):
        if state.get('failed_units'):
            calls.append(dict(state['failed_units']))
    async def noop(*args):
        pass
    with pytest.raises(RuntimeError, match='storyboard_input_budget'):
        asyncio.run(generate(req, lambda: pytest.fail('must not call model'), plan=[], durations=[8],
            state={}, save=save, progress=noop, script='甲开门。', batch_attempts=3))
    assert len(calls) == 1


def test_failed_page_resumes_missing_source_only():
    req = automatic_request(request(), rules='保留台词', references='风格')
    state, calls = {}, []
    fail = True

    class Runtime:
        async def run(self, current):
            nonlocal fail
            assert current.tool_mode == 'none'
            spans = json.loads(current.prompt.split('待覆盖原文：')[1].split('\n已完成片段数：')[0])
            calls.append([s['text'] for s in spans])
            if len(calls) == 2 and fail:
                fail = False
                raise RuntimeError('provider offline')
            return SimpleNamespace(final_response=json.dumps({'shots': [
                {**shot(1), 'source_ids': [spans[0]['id']]}]}), manifest={})

    async def noop(*args):
        pass

    async def run():
        return await generate(req, Runtime, plan=[], durations=[8], state=state,
            save=noop, progress=noop, script='甲开门。乙关窗。', batch_attempts=1)

    with pytest.raises(RuntimeError, match='provider offline'):
        asyncio.run(run())
    assert sum(map(len, state['generation_pages'].values())) == 1
    result, _ = asyncio.run(run())
    assert len(json.loads(result)['shots']) == 2
    assert calls == [['甲开门。', '乙关窗。'], ['乙关窗。'], ['乙关窗。']]
    assert all(not item['missing'] for item in state['source_coverage'].values())


def test_context_supplement_is_bounded_and_no_tools():
    req = automatic_request(request(), rules='保留台词', references='# 风格\n逆光')
    identifier = prepare(req, {})['sections'][0]['id']
    calls = []

    class Runtime:
        async def run(self, current):
            calls.append(current)
            return SimpleNamespace(final_response=json.dumps({'needs_context': [identifier]}), manifest={})

    async def noop(*args):
        pass

    with pytest.raises(RuntimeError, match='重复'):
        asyncio.run(generate(req, Runtime, plan=[], durations=[8], state={},
            save=noop, progress=noop, script='开门。', batch_attempts=1))
    assert len(calls) == 2 and all(c.tool_mode == 'none' for c in calls)


def test_source_mapping_repairs_only_mapping_and_preserves_generated_content():
    original = [shot(1), shot(2)]
    units = [{'id': 'a', 'text': '开门'}, {'id': 'b', 'text': '转身'}]
    req = mapping_request(request(), original, units)
    assert req.tool_mode == 'none' and not req.project_files
    merged = merge_mapping(original, units, json.dumps({'mappings': [
        {'row': 2, 'source_ids': ['b']}, {'row': 1, 'source_ids': ['a']}]}))
    assert [{k: v for k, v in r.items() if k != 'source_ids'} for r in merged] == original
    assert not coverage(merged, units)
    with pytest.raises(ValueError):
        merge_mapping(original, units, '{"mappings":[{"row":1,"source_ids":["a"]}]}')


def test_mapping_failure_retries_mapping_not_generation():
    req = automatic_request(request(), rules='保留台词')
    state, calls = {}, []
    spans = []

    class Runtime:
        async def run(self, current):
            nonlocal spans
            if '待覆盖原文：' in current.prompt:
                calls.append('generation')
                spans = json.loads(current.prompt.split('待覆盖原文：')[1].split('\n已完成片段数：')[0])
                value = {'shots': [shot(1)]}
            else:
                calls.append('mapping')
                if calls.count('mapping') == 1:
                    raise RuntimeError('temporary mapping failure')
                value = {'mappings': [{'row': 1, 'source_ids': [s['id'] for s in spans]}]}
            return SimpleNamespace(final_response=json.dumps(value), manifest={})

    async def noop(*args):
        pass

    result, _ = asyncio.run(generate(req, Runtime, plan=[], durations=[8], state=state,
        save=noop, progress=noop, script='开门。', batch_attempts=2))
    assert len(json.loads(result)['shots']) == 1
    assert calls == ['generation', 'mapping', 'mapping']
