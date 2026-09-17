import { test } from 'node:test'
import assert from 'node:assert/strict'
import { prepareChatImage } from '../src/lib/imageUpload.ts'

test('sequential images release workers; failed compression does not block the next image', async () => {
  const workers = []
  globalThis.OffscreenCanvas = class {}
  globalThis.Worker = class {
    constructor() { workers.push(this) }
    postMessage(file) {
      queueMicrotask(() => this.onmessage({ data: file.name === 'bad.png'
        ? { error: true } : { blob: new Blob(['webp'], { type: 'image/webp' }) } }))
    }
    terminate() { this.terminated = true }
  }
  for (const name of ['one.png', 'bad.png', 'two.png']) {
    const source = new File(['original image bytes'], name, { type: 'image/png' })
    const output = await prepareChatImage(source, new AbortController().signal)
    assert.equal(output.type, name === 'bad.png' ? 'image/png' : 'image/webp')
    assert.ok(output.size <= source.size)
    assert.ok(workers.at(-1).terminated)
  }
  assert.equal(workers.length, 3)
})

test('cancel terminates the worker immediately and next upload still succeeds', async () => {
  let worker
  globalThis.Worker = class {
    constructor() { worker = this }
    postMessage() {}
    terminate() { this.terminated = true }
  }
  const controller = new AbortController()
  const source = new File(['image'], 'photo.png')
  const pending = prepareChatImage(source, controller.signal)
  controller.abort()
  await assert.rejects(pending, { name: 'AbortError' })
  assert.ok(worker.terminated)
  const next = prepareChatImage(source, new AbortController().signal)
  worker.onmessage({ data: { error: true } })
  assert.equal(await next, source)
})
