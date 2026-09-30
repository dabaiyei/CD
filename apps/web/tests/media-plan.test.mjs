import test from 'node:test'
import assert from 'node:assert/strict'
import { mediaPlanView } from '../src/lib/mediaPlanView.ts'

const plan = { source_message_id: 'draft', index: 1, auto_continue: true,
  steps: [{ type: 'image', options: { aspect_ratio: '9:16' } },
    { type: 'video', options: { duration_seconds: 12 }, use_previous_image: true }],
  completed: [{ index: 0 }], status: 'ready' }
const task = (status) => ({ status, latest_message: '正在核验首帧',
  request_payload: { jev_route: { media_plan: { ...plan, index: 1 } } } })

test('continuation displays completed image and current video stage', () => {
  for (const status of ['queued', 'running', 'failed', 'cancelled', 'succeeded']) {
    const view = mediaPlanView(plan, task(status))
    assert.equal(view.steps[0].status, 'done')
    assert.equal(view.steps[1].status, { succeeded: 'done', cancelled: 'paused' }[status] || status)
    assert.equal(view.steps[1].dependency, '使用上一步生成图')
    assert.equal(view.done, status === 'succeeded' ? 2 : 1)
  }
})
test('unrelated task never changes saved plan or displays its error', () => {
  const other = task('failed')
  other.request_payload.jev_route.media_plan.source_message_id = 'other'
  const view = mediaPlanView(plan, other)
  assert.equal(view.steps[1].status, 'ready')
  assert.equal(view.failed, false)
  assert.equal(view.detail, '')
})
test('finished plans survive refresh without active tasks', () => {
  const view = mediaPlanView({ ...plan, index: 2, status: 'completed' }, null)
  assert.equal(view.title, '连续创作已完成')
  assert.equal(view.done, 2)
  assert.equal(mediaPlanView(null, null), null)
})
test('a text reply cancelling a plan does not mark media as generated', () => {
  const cancelled = task('succeeded')
  cancelled.request_payload.jev_route.output = 'text'
  const view = mediaPlanView({ ...plan, status: 'cancelled' }, cancelled)
  assert.equal(view.steps[1].status, 'paused')
  assert.equal(view.done, 1)
})
