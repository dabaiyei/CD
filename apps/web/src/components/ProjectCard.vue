<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { ArrowUpRight, Settings2, Trash2 } from 'lucide-vue-next'
import gsap from 'gsap'

import type { Project } from '@/types'

defineProps<{ project: Project }>()
const emit = defineEmits<{ open: []; settings: []; delete: [] }>()

const cardRoot = ref<HTMLElement | null>(null)
type QuickTo = ReturnType<typeof gsap.quickTo>
let rotateX: QuickTo | undefined
let rotateY: QuickTo | undefined
let lift: QuickTo | undefined
let motionMedia: gsap.MatchMedia | undefined

function handlePointerMove(event: PointerEvent): void {
  const card = cardRoot.value
  if (!card || !rotateX || !rotateY) return
  const bounds = card.getBoundingClientRect()
  const x = (event.clientX - bounds.left) / bounds.width - 0.5
  const y = (event.clientY - bounds.top) / bounds.height - 0.5
  rotateX(y * -3)
  rotateY(x * 3)
}

function handlePointerEnter(): void {
  lift?.(-3)
}

function handlePointerLeave(): void {
  rotateX?.(0)
  rotateY?.(0)
  lift?.(0)
}

onMounted(() => {
  const card = cardRoot.value
  if (!card) return

  motionMedia = gsap.matchMedia()
  motionMedia.add('(hover: hover) and (pointer: fine) and (prefers-reduced-motion: no-preference)', () => {
    gsap.set(card, { transformPerspective: 900, transformOrigin: 'center center' })
    const options = { duration: 0.34, ease: 'power3.out', overwrite: 'auto' as const }
    rotateX = gsap.quickTo(card, 'rotationX', options)
    rotateY = gsap.quickTo(card, 'rotationY', options)
    lift = gsap.quickTo(card, 'y', { duration: 0.24, ease: 'power3.out', overwrite: 'auto' })

    return () => {
      rotateX = undefined
      rotateY = undefined
      lift = undefined
      gsap.killTweensOf(card)
      gsap.set(card, { clearProps: 'transform,transformOrigin' })
    }
  })
})

onBeforeUnmount(() => motionMedia?.revert())
</script>

<template>
  <article
    ref="cardRoot"
    class="project-card"
    tabindex="0"
    @click="emit('open')"
    @keydown.enter="emit('open')"
    @pointerenter="handlePointerEnter"
    @pointermove="handlePointerMove"
    @pointerleave="handlePointerLeave"
  >
    <div class="project-card__media">
      <img
        :src="project.cover_url || '/covers/login-studio.jpg'"
        :alt="`${project.name}项目封面`"
        loading="lazy"
      />
      <div class="project-card__actions">
        <button class="media-action" type="button" title="项目设置" aria-label="项目设置" @click.stop="emit('settings')">
          <Settings2 :size="16" />
        </button>
        <button class="media-action media-action--danger" type="button" title="删除项目" aria-label="删除项目" @click.stop="emit('delete')">
          <Trash2 :size="16" />
        </button>
      </div>
      <span class="project-card__ratio tabular-nums">{{ project.aspect_ratio }}</span>
    </div>
    <div class="project-card__body">
      <div class="project-card__heading">
        <h2>{{ project.name }}</h2>
        <ArrowUpRight :size="17" aria-hidden="true" />
      </div>
      <p>{{ project.description || '尚未填写项目简介' }}</p>
      <div class="project-card__meta">
        <span>{{ project.video_resolution }}</span>
        <span>{{ project.image_resolution }}</span>
        <time :datetime="project.updated_at">{{ new Date(project.updated_at).toLocaleDateString('zh-CN') }}</time>
      </div>
    </div>
  </article>
</template>
