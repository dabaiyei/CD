<script setup lang="ts" generic="T extends { id: string }">
import { useVirtualizer, useWindowVirtualizer } from '@tanstack/vue-virtual'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { ComponentPublicInstance } from 'vue'

/**
 * Row-virtualized responsive card grid. Cards keep the responsive
 * `repeat(auto-fill, minmax(minColumnWidth, 1fr))` layout, but only the rows
 * near the viewport are mounted, so chapters with hundreds of assets or shots
 * stay responsive.
 *
 * The grid follows whichever ancestor actually scrolls: the inner canvas on
 * desktop (`overflow-y: auto`) and the document on H5 (`overflow: visible`).
 */
const props = withDefaults(defineProps<{
  items: T[]
  minColumnWidth?: number
  columnGap?: number
  rowGap?: number
  estimateRowHeight?: number
}>(), {
  minColumnWidth: 150,
  columnGap: 12,
  rowGap: 12,
  estimateRowHeight: 200,
})

defineSlots<{ default(props: { item: T; index: number }): unknown }>()

const container = ref<HTMLElement | null>(null)
const containerWidth = ref(0)
const scrollMargin = ref(0)
const scrollElement = ref<HTMLElement | null>(null)
const usesWindowScroll = computed(() => scrollElement.value === null)

const columns = computed(() => {
  // Narrow dialogs used to force two columns via a media query; keep that
  // behavior instead of collapsing to a single wide card.
  if (containerWidth.value && containerWidth.value < props.minColumnWidth * 2.6) return 2
  return Math.max(
    1,
    Math.floor((containerWidth.value + props.columnGap) / (props.minColumnWidth + props.columnGap)),
  )
})
const rowCount = computed(() => Math.ceil(props.items.length / columns.value))
const rowSize = computed(() => props.estimateRowHeight + props.rowGap)

const elementVirtualizer = useVirtualizer<HTMLElement, HTMLElement>(computed(() => ({
  count: usesWindowScroll.value ? 0 : rowCount.value,
  getScrollElement: () => scrollElement.value,
  estimateSize: () => rowSize.value,
  overscan: 3,
  getItemKey: (index: number) => index,
})))

const windowVirtualizer = useWindowVirtualizer(computed(() => ({
  count: usesWindowScroll.value ? rowCount.value : 0,
  estimateSize: () => rowSize.value,
  overscan: 3,
  scrollMargin: scrollMargin.value,
  getItemKey: (index: number) => index,
})))

const virtualizer = computed(() => (usesWindowScroll.value ? windowVirtualizer.value : elementVirtualizer.value))
const totalSize = computed(() => virtualizer.value.getTotalSize())
const virtualRows = computed(() => {
  const perRow = columns.value
  return virtualizer.value.getVirtualItems().flatMap((virtualRow) => {
    const offset = virtualRow.index * perRow
    const items = props.items.slice(offset, offset + perRow)
    return items.length ? [{ virtualRow, items, offset }] : []
  })
})

function rowOffset(start: number): number {
  return usesWindowScroll.value ? Math.max(0, start - scrollMargin.value) : start
}

function measureRow(element: Element | ComponentPublicInstance | null): void {
  const node = element instanceof Element ? element : element?.$el
  if (node instanceof HTMLElement) virtualizer.value.measureElement(node)
}

let resizeObserver: ResizeObserver | undefined

function readLayout(): void {
  const element = container.value
  if (!element) return
  containerWidth.value = element.clientWidth
  scrollMargin.value = element.getBoundingClientRect().top + window.scrollY
  scrollElement.value = findScrollParent(element)
}

/** Walk up to the first ancestor that actually scrolls; null means the page. */
function findScrollParent(element: HTMLElement): HTMLElement | null {
  // A grid can be its own scroller (the asset library dialog does this).
  const own = getComputedStyle(element)
  if (own.overflowY === 'auto' || own.overflowY === 'scroll') return element
  let current = element.parentElement
  while (current) {
    const overflowY = getComputedStyle(current).overflowY
    if (overflowY === 'auto' || overflowY === 'scroll') return current
    current = current.parentElement
  }
  return null
}

onMounted(() => {
  readLayout()
  resizeObserver = new ResizeObserver(readLayout)
  if (container.value) resizeObserver.observe(container.value)
})

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  resizeObserver = undefined
})

watch(columns, async () => {
  await nextTick()
  virtualizer.value.measure()
  readLayout()
})
</script>

<template>
  <div ref="container" class="virtual-grid">
    <div class="virtual-grid__canvas" :style="{ height: `${totalSize}px` }">
      <div
        v-for="row in virtualRows"
        :key="row.virtualRow.index"
        :ref="measureRow"
        :data-index="row.virtualRow.index"
        class="virtual-grid__row"
        :style="{
          transform: `translateY(${rowOffset(row.virtualRow.start)}px)`,
          gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))`,
          columnGap: `${columnGap}px`,
          paddingBottom: `${rowGap}px`,
        }"
      >
        <template v-for="(item, index) in row.items" :key="item.id">
          <slot :item="item" :index="row.offset + index" />
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.virtual-grid {
  position: relative;
  display: block;
  width: 100%;
}

.virtual-grid__canvas {
  position: relative;
  width: 100%;
}

.virtual-grid__row {
  position: absolute;
  top: 0;
  left: 0;
  display: grid;
  width: 100%;
  box-sizing: border-box;
  align-items: start;
}
</style>
