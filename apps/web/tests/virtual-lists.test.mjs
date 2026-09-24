import { test } from 'node:test'
import assert from 'node:assert/strict'
import { Virtualizer } from '@tanstack/vue-virtual'
import { chapterExtent, gridGeometry, scrollOrigin } from '../src/lib/virtualLayout.ts'

// Exercise the installed virtualizer with scroll/resize callbacks, without a
// browser or provider calls. Counts mirror long projects, not a ten-row demo.
function viewport({ count, size, horizontal = false, margin = 0, width = 390, height = 700 }) {
  let onScroll
  const element = { ownerDocument: { defaultView: {} }, clientWidth: width, clientHeight: height,
    scrollWidth: horizontal ? count * size : width, scrollHeight: margin + count * size }
  const instance = new Virtualizer({
    count, estimateSize: () => size, horizontal, scrollMargin: margin, overscan: 2,
    getScrollElement: () => element,
    observeElementRect: (_, callback) => { callback({ width, height }); return () => {} },
    observeElementOffset: (_, callback) => { onScroll = callback; callback(0, false); return () => {} },
    scrollToFn: () => {},
  })
  const cleanup = instance._didMount()
  instance._willUpdate()
  return { instance, cleanup, scroll: offset => onScroll(offset, true) }
}

test('narrow panels never force two overflowing columns; card height matches the media ratio', () => {
  for (const width of [180, 260, 320, 390, 640, 1100]) {
    for (const ratio of [4 / 3, 16 / 9]) {
      const grid = gridGeometry(width, 150, 12, ratio, 76)
      assert.ok(grid.columns >= 1)
      assert.ok(grid.cardWidth >= Math.min(150, width))
      assert.ok(Math.abs(grid.columns * grid.cardWidth + (grid.columns - 1) * 12 - width) < .01)
      assert.equal(grid.cardHeight, grid.cardWidth / ratio + 76)
    }
  }
  assert.equal(gridGeometry(260, 150, 12).columns, 1)
})

test('1000 media cards remain reachable below a long detail panel with a bounded mounted range', () => {
  for (const width of [320, 640, 1100]) {
    const grid = gridGeometry(width, 150, 12, 16 / 9, 56)
    const count = Math.ceil(1000 / grid.columns)
    const size = grid.cardHeight + 12
    const margin = 1850
    const view = viewport({ count, size, margin, width })
    for (const target of [0, 10, 100, count - 1]) {
      view.scroll(margin + target * size)
      const rows = view.instance.getVirtualItems()
      assert.ok(rows.some(row => row.index === target), `missing row ${target}`)
      assert.ok(rows.length < 15, `${rows.length} rows mounted at ${width}px`)
      const row = rows.find(row => row.index === target)
      assert.ok(Math.abs(row.start - margin - target * size) < .1)
    }
    assert.ok(Math.abs(view.instance.getTotalSize() - count * size) < .1)
    view.cleanup()
  }
})

test('scroll origin stays constant during scrolling and follows growing details and borders', () => {
  assert.equal(scrollOrigin(900, 100, 200, 2), 998)
  assert.equal(scrollOrigin(700, 100, 400, 2), 998)
  assert.equal(scrollOrigin(1100, 100, 400, 2), 1398)
  assert.equal(scrollOrigin(700, 0, 400), 1100) // document scroll
  assert.equal(scrollOrigin(92, 90, 0, 2), 0) // grid owns its scrollbar
})

test('chapter axis remount never reuses vertical sizes as horizontal widths', () => {
  for (const [horizontal, width] of [[true, 390], [false, 190], [true, 360], [false, 190]]) {
    const size = chapterExtent(horizontal, width)
    const view = viewport({ count: 2000, size, horizontal, width })
    view.scroll(1450 * size)
    const rows = view.instance.getVirtualItems()
    assert.ok(rows.some(row => row.index === 1450))
    assert.ok(rows.length < 15)
    assert.ok(rows.every(row => row.size === size))
    for (let i = 1; i < rows.length; i++) assert.equal(rows[i].start, rows[i - 1].end)
    view.scroll(1999 * size)
    assert.equal(view.instance.getVirtualItems().at(-1).index, 1999)
    view.cleanup()
  }
})

test('column reflow preserves the visible item and recalculates offsets', () => {
  const item = 600
  for (const width of [390, 844, 360]) {
    const grid = gridGeometry(width, 150, 12, 4 / 3, 76)
    const row = Math.floor(item / grid.columns)
    const view = viewport({ count: Math.ceil(1001 / grid.columns), size: grid.cardHeight + 12, width })
    view.scroll(row * (grid.cardHeight + 12))
    assert.ok(view.instance.getVirtualItems().some(entry =>
      entry.index * grid.columns <= item && (entry.index + 1) * grid.columns > item))
    view.cleanup()
  }
})
