import { test } from 'node:test'
import assert from 'node:assert/strict'
import { reactive } from 'vue'
import { copyPlan, taskPlan } from '../src/lib/replicaPlan.ts'

test('saved and polled reactive plans can be edited without mutating task results', () => {
  const original = { summary: '分析完成', shots: [{ start: 0, end: 12, observation: '画面', prompt: '原提示词' }] }
  for (const source of [original, reactive(original)]) {
    const edit = copyPlan(source)
    assert.deepEqual(edit, original)
    edit.shots[0].prompt = '新的提示词'
    edit.shots.push({ start: 12, end: 24, observation: '下段', prompt: '后续' })
    assert.equal(source.shots[0].prompt, '原提示词')
    assert.equal(source.shots.length, 1)
  }
})

test('render jobs restore their submitted plan; failed analyses expose saved batches', () => {
  const plan = { summary: '已分析', shots: [{ start: 0, end: 12, observation: '画面', prompt: '动作' }] }
  assert.deepEqual(taskPlan({ request_payload: { options: { plan } }, result_payload: {} }), plan)
  const partial = taskPlan({ request_payload: {}, result_payload: { batches: { '1': { ...plan, summary: '后段' }, '0': plan } } })
  assert.equal(partial.shots.length, 2)
  assert.equal(partial.summary, '已分析\n后段')
  assert.equal(taskPlan({ request_payload: {}, result_payload: null }), null)
})

test('saved editor drafts restore continuity controls instead of stale analysis', () => {
  const original = { summary: 'old', shots: [{ start: 0, end: 4, observation: 'scene', prompt: 'old' }] }
  const draft = reactive({ summary: 'edited', shots: [{ ...original.shots[0], prompt: 'new', scene_id: 'room', boundary: 'continuous' }] })
  const restored = taskPlan({ request_payload: { editor_draft: { plan: draft } }, result_payload: { plan: original } })
  assert.equal(restored.shots[0].boundary, 'continuous')
  assert.equal(restored.shots[0].scene_id, 'room')
  restored.shots[0].boundary = 'cut'
  assert.equal(draft.shots[0].boundary, 'continuous')
  assert.equal(restored.summary, 'edited')
})
