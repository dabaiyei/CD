<script setup lang="ts">
import { useVirtualizer } from '@tanstack/vue-virtual'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RotateCcw, Trash2 } from 'lucide-vue-next'
import { chapterExtent } from '@/lib/virtualLayout'
import type { Chapter, ChapterStatus } from '@/types'

// The parent keys this component by axis. A rotated screen gets a fresh
// scroll observer and cache instead of reusing vertical offsets as widths.
const props = defineProps<{
  chapters: Chapter[]
  selectedId?: string
  horizontal: boolean
  busy: boolean
  statusLabels: Record<ChapterStatus, string>
}>()
const emit = defineEmits<{
  select: [id: string]
  action: [chapter: Chapter, mode: 'redo' | 'delete']
}>()
const viewport = ref<HTMLElement | null>(null)
const width = ref(0)
const extent = computed(() => chapterExtent(props.horizontal, width.value))
const virtualizer = useVirtualizer<HTMLElement, HTMLElement>(computed(() => {
  const size = extent.value
  const ids = props.chapters.map(chapter => chapter.id)
  return {
    count: ids.length,
    getScrollElement: () => viewport.value,
    getItemKey: (index: number) => ids[index] ?? index,
    estimateSize: () => size,
    horizontal: props.horizontal,
    overscan: 3,
  }
}))
const rows = computed(() => virtualizer.value.getVirtualItems().flatMap(row => {
  const chapter = props.chapters[row.index]
  return chapter ? [{ row, chapter }] : []
}))
const totalSize = computed(() => virtualizer.value.getTotalSize())
let observer: ResizeObserver | undefined
let frame = 0
let disposed = false

async function scrollToSelected() {
  await nextTick()
  if (disposed) return
  const index = props.chapters.findIndex(chapter => chapter.id === props.selectedId)
  if (index >= 0) virtualizer.value.scrollToIndex(index, { align: 'auto' })
}
function resize() {
  if (frame || disposed) return
  frame = requestAnimationFrame(async () => {
    frame = 0
    const currentWidth = viewport.value?.clientWidth ?? 0
    if (width.value === currentWidth) return
    width.value = currentWidth
    await nextTick()
    if (disposed) return
    virtualizer.value.measure()
    void scrollToSelected()
  })
}
onMounted(() => {
  observer = new ResizeObserver(resize)
  if (viewport.value) observer.observe(viewport.value)
  resize()
  void scrollToSelected()
})
onBeforeUnmount(() => {
  disposed = true
  observer?.disconnect()
  cancelAnimationFrame(frame)
})
watch(() => [props.selectedId, props.chapters.length], scrollToSelected, { flush: 'post' })
defineExpose({ scrollToSelected })
</script>

<template>
  <nav ref="viewport" class="chapter-browser" :class="{ 'chapter-browser--horizontal': horizontal }" aria-label="章节列表">
    <div class="chapter-browser__track" :style="horizontal ? { width: `${totalSize}px` } : { height: `${totalSize}px` }">
      <div v-for="{ row, chapter } in rows" :key="chapter.id" class="chapter-browser__row"
        :style="horizontal
          ? { width: `${row.size}px`, height: '96px', transform: `translateX(${row.start}px)` }
          : { width: '100%', height: `${row.size}px`, transform: `translateY(${row.start}px)` }">
        <article class="chapter-browser__card" :class="{ 'is-active': selectedId === chapter.id }">
          <button type="button" class="chapter-browser__select" :disabled="chapter.locked"
            :aria-current="selectedId === chapter.id ? 'true' : undefined" :title="chapter.title" @click="emit('select', chapter.id)">
            <span class="chapter-browser__number">{{ String(chapter.order_index).padStart(2, '0') }}</span>
            <strong>{{ chapter.title }}</strong>
          </button>
          <footer>
            <small :title="chapter.locked ? '完成前章剧本后解锁' : statusLabels[chapter.status]">{{ chapter.locked ? '待解锁' : statusLabels[chapter.status] }}</small>
            <div class="chapter-browser__actions">
              <button type="button" :aria-label="`重做章节：${chapter.title}`" title="重做章节"
                :disabled="busy || chapter.locked" @click="emit('action', chapter, 'redo')"><RotateCcw :size="15" /></button>
              <button type="button" class="is-danger" :aria-label="`删除章节：${chapter.title}`" title="删除章节"
                :disabled="busy || chapter.locked" @click="emit('action', chapter, 'delete')"><Trash2 :size="15" /></button>
            </div>
          </footer>
        </article>
      </div>
    </div>
  </nav>
</template>

<style scoped>
.chapter-browser {
  min-width: 0; min-height: 0; width: 100%; height: calc(100% - 46px);
  padding: 8px; box-sizing: border-box; overflow-x: hidden; overflow-y: auto;
  scrollbar-width: thin; overflow-anchor: none;
}
.chapter-browser--horizontal { height: 114px; overflow-x: auto; overflow-y: hidden; }
.chapter-browser__track { position: relative; width: 100%; }
.chapter-browser--horizontal .chapter-browser__track { height: 96px; }
.chapter-browser__row { position: absolute; top: 0; left: 0; padding: 0 0 8px; box-sizing: border-box; transition: none; }
.chapter-browser--horizontal .chapter-browser__row { padding-right: 8px; }
.chapter-browser__card {
  height: 88px; min-width: 0; border-radius: 12px;
  background: var(--glass-inset, var(--surface-subtle));
  box-shadow: inset 0 0 0 1px var(--glass-divider, var(--line));
  color: var(--ink-secondary);
}
.chapter-browser__card.is-active {
  background: color-mix(in srgb, var(--brand) 10%, var(--glass-inset, var(--surface-subtle)));
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--brand) 42%, var(--line)); color: var(--ink);
}
.chapter-browser__select {
  display: flex; align-items: center; gap: 7px; width: 100%; height: 46px; min-width: 0;
  padding: 4px 10px; border: 0; border-radius: 12px 12px 0 0; background: transparent;
  color: inherit; text-align: left; cursor: pointer;
}
.chapter-browser__select strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }
.chapter-browser__number { flex: 0 0 auto; font-size: 10px; font-variant-numeric: tabular-nums; color: var(--ink-tertiary); }
.chapter-browser__card footer { display: flex; align-items: center; justify-content: space-between; height: 40px; padding: 0 4px 0 10px; gap: 4px; }
.chapter-browser__card small { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 10px; color: var(--ink-tertiary); }
.chapter-browser__actions { display: flex; flex: 0 0 auto; }
.chapter-browser__actions button {
  display: grid; place-items: center; width: 40px; height: 40px; padding: 0;
  border: 0; border-radius: 8px; background: transparent; color: var(--ink-secondary); cursor: pointer;
  transition: color 150ms, background-color 150ms;
}
.chapter-browser button:hover:not(:disabled) { color: var(--brand); background: var(--brand-soft); }
.chapter-browser .is-danger:hover:not(:disabled) { color: #e05d5d; background: color-mix(in srgb, #e05d5d 12%, transparent); }
.chapter-browser button:focus-visible { outline: 2px solid var(--brand); outline-offset: -2px; }
.chapter-browser button:disabled { opacity: .5; cursor: not-allowed; }
</style>
