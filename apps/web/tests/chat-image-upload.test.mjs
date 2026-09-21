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

test('phone photo extensions and MIME variants are recognized as images', async () => {
  const { isSupportedImage, IMAGE_ACCEPT_ATTRIBUTE } = await import('../src/lib/imageUpload.ts')
  // Phones and Windows apps save JPEGs under several extensions; some browsers
  // report an empty or unusual MIME type for them.
  for (const name of ['PHOTO.JPG', 'a.jpeg', 'b.jpe', 'c.JFIF', 'IMG_0001.mpo', 'd.png', 'e.WEBP']) {
    assert.ok(isSupportedImage(new File(['x'], name)), `${name} should be supported`)
  }
  for (const type of ['image/jpeg', 'image/jpg', 'image/pjpeg', 'image/png', 'image/webp']) {
    assert.ok(isSupportedImage(new File(['x'], 'noext', { type })), `${type} should be supported`)
  }
  // Files with no usable type or extension still fail the fast pre-check.
  assert.equal(isSupportedImage(new File(['x'], 'mystery', { type: '' })), false)
  assert.equal(isSupportedImage(new File(['x'], 'photo.gif', { type: 'image/gif' })), false)
  assert.equal(isSupportedImage(new File(['x'], 'doc.pdf', { type: 'application/pdf' })), false)
  // Pickers must offer the same extensions the pre-check accepts.
  for (const ext of ['.jpg', '.jpeg', '.jpe', '.jfif', '.mpo', '.png', '.webp']) {
    assert.ok(IMAGE_ACCEPT_ATTRIBUTE.includes(ext), `${ext} missing from accept attribute`)
  }
})
