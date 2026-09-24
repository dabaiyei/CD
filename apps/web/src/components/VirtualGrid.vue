<script setup lang="ts" generic="T extends { id: string }">
import { useVirtualizer, useWindowVirtualizer } from '@tanstack/vue-virtual'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import type { ComponentPublicInstance } from 'vue'
import { gridGeometry, scrollOrigin } from '@/lib/virtualLayout'

const props = withDefaults(defineProps<{
  items: T[]
  minColumnWidth?: number
  columnGap?: number
  rowGap?: number
  estimateRowHeight?: number
  /** Uniform media cards need no per-row ResizeObserver or height correction. */
  mediaAspectRatio?: number
  cardFooterHeight?: number
}>(), {
  minColumnWidth: 150, columnGap: 12, rowGap: 12, estimateRowHeight: 200,
  mediaAspectRatio: 0, cardFooterHeight: 0,
})
defineSlots<{ default(props: { item: T; index: number }): unknown }>()

const container = ref<HTMLElement | null>(null)
const canvas = ref<HTMLElement | null>(null)
const containerWidth = ref(0)
const scrollMargin = ref(0)
const scrollElement = shallowRef<HTMLElement | null>(null)
const ready = ref(false)
const usesWindowScroll = computed(() => scrollElement.value === null)
const geometry = computed(() => gridGeometry(containerWidth.value, props.minColumnWidth,
  props.columnGap, props.mediaAspectRatio, props.cardFooterHeight))
const columns = computed(() => geometry.value.columns)
const fixed = computed(() => props.mediaAspectRatio > 0)
const rowCount = computed(() => Math.ceil(props.items.length / columns.value))
const rowSize = computed(() => (fixed.value ? geometry.value.cardHeight : props.estimateRowHeight) + props.rowGap)
const rowKeys = computed(() => Array.from({ length: rowCount.value }, (_, index) =>
  `${columns.value}:${props.items[index * columns.value]?.id ?? index}`))
const options = computed(() => {
  const size = rowSize.value
  const keys = rowKeys.value
  return {
    count: rowCount.value,
    estimateSize: () => size,
    overscan: 2,
    scrollMargin: scrollMargin.value,
    getItemKey: (index: number) => keys[index] ?? index,
    useAnimationFrameWithResizeObserver: true,
  }
})
const elementVirtualizer = useVirtualizer<HTMLElement, HTMLElement>(computed(() => ({
  ...options.value,
  enabled: ready.value && !usesWindowScroll.value,
  getScrollElement: () => scrollElement.value,
})))
const windowVirtualizer = useWindowVirtualizer(computed(() => ({
  ...options.value,
  enabled: ready.value && usesWindowScroll.value,
})))

// Read the library's shallow ref directly: a computed returning the same
// instance would swallow its triggerRef updates and freeze the rendered range.
function getVirtualizer() {
  return usesWindowScroll.value ? windowVirtualizer.value : elementVirtualizer.value
}
const totalSize = computed(() => getVirtualizer().getTotalSize())
const virtualRows = computed(() => getVirtualizer().getVirtualItems().map((virtualRow) => {
  const offset = virtualRow.index * columns.value
  return { virtualRow, offset, items: props.items.slice(offset, offset + columns.value) }
}))
function measureRow(element: Element | ComponentPublicInstance | null): void {
  if (fixed.value) return
  const node = element instanceof Element ? element : element?.$el
  if (node instanceof HTMLElement) getVirtualizer().measureElement(node)
}

let observer: ResizeObserver | undefined
let frame = 0
let disposed = false
let discoverParent = true

function findScrollParent(element: HTMLElement): HTMLElement | null {
  // Start at the grid itself: asset dialogs can make it the scroll container.
  for (let current: HTMLElement | null = element; current; current = current.parentElement) {
    if (current === document.body || current === document.documentElement) return null
    if (/^(auto|scroll)$/.test(getComputedStyle(current).overflowY)) return current
  }
  return null
}

async function readLayout() {
  frame = 0
  if (disposed || !container.value || !canvas.value) return
  const previous = getVirtualizer()
  const localOffset = (previous.scrollOffset ?? 0) - scrollMargin.value
  const withinGrid = ready.value && localOffset >= 0 && localOffset < totalSize.value
  const anchor = previous.getVirtualItems().find(row => row.end > (previous.scrollOffset ?? 0))
  const anchorItem = (anchor?.index ?? 0) * columns.value
  const width = canvas.value.clientWidth
  const oldParent = scrollElement.value
  if (discoverParent) {
    discoverParent = false
    scrollElement.value = findScrollParent(container.value)
    observer?.disconnect()
    // Watching enclosing sections catches a video detail growing above the
    // grid. Watching only the grid misses its changing scroll origin.
    for (let node: HTMLElement | null = container.value; node; node = node.parentElement) {
      observer?.observe(node)
      if (node === scrollElement.value || node === document.body) break
    }
  }
  const parent = scrollElement.value
  scrollMargin.value = scrollOrigin(canvas.value.getBoundingClientRect().top,
    parent?.getBoundingClientRect().top ?? 0, parent?.scrollTop ?? window.scrollY, parent?.clientTop ?? 0)
  const resized = width !== containerWidth.value || parent !== oldParent
  containerWidth.value = width
  ready.value = width > 0
  if (resized) {
    await nextTick()
    if (disposed) return
    getVirtualizer().measure()
    await nextTick()
    if (!disposed && withinGrid && props.items.length) {
      getVirtualizer().scrollToIndex(Math.floor(Math.min(anchorItem, props.items.length - 1) / columns.value), { align: 'start' })
    }
  }
}

function scheduleLayout() {
  if (!disposed && !frame) frame = requestAnimationFrame(() => { void readLayout() })
}
function onViewportResize() {
  discoverParent = true
  scheduleLayout()
}
onMounted(() => {
  observer = new ResizeObserver(scheduleLayout)
  void readLayout()
  window.addEventListener('resize', onViewportResize, { passive: true })
})
onBeforeUnmount(() => {
  disposed = true
  cancelAnimationFrame(frame)
  observer?.disconnect()
  window.removeEventListener('resize', onViewportResize)
})
watch(() => props.items.length, scheduleLayout, { flush: 'post' })
</script>

<template>
  <div ref="container" class="virtual-grid" :class="{ 'virtual-grid--fixed': fixed }">
    <div ref="canvas" class="virtual-grid__canvas" :style="{ height: `${totalSize}px` }">
      <div v-for="row in virtualRows" :key="String(row.virtualRow.key)"
        :ref="fixed ? undefined : measureRow" :data-index="row.virtualRow.index"
        class="virtual-grid__row"
        :style="{
          transform: `translateY(${row.virtualRow.start - scrollMargin}px)`,
          gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))`,
          columnGap: `${columnGap}px`, paddingBottom: `${rowGap}px`,
          height: fixed ? `${rowSize}px` : undefined,
        }">
        <template v-for="(item, index) in row.items" :key="item.id">
          <slot :item="item" :index="row.offset + index" />
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.virtual-grid { position: relative; display: block; width: 100%; min-width: 0; overflow-anchor: none; }
.virtual-grid__canvas { position: relative; width: 100%; }
.virtual-grid__row {
  position: absolute; top: 0; left: 0; display: grid; width: 100%;
  box-sizing: border-box; align-items: start;
  transition: none;
}
.virtual-grid--fixed .virtual-grid__row { contain: layout style; }
</style>
