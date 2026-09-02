<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { gsap } from 'gsap'
import {
  BrainCircuit,
  FilePenLine,
  FileSearch,
  FileText,
  LoaderCircle,
  Search,
  Sparkles,
  Wrench,
} from 'lucide-vue-next'

import { prefersReducedMotion } from '@/lib/motion'
import type { AgentExecutionStep } from '@/types'

const props = withDefaults(defineProps<{
  steps: AgentExecutionStep[]
  active?: boolean
}>(), {
  active: false,
})

const displayedLabel = ref('')
const typing = ref(false)
const activeStep = computed(() => [...props.steps].reverse().find((step) => step.status === 'running'))

let textTimeline: gsap.core.Timeline | null = null
let stopLabelWatch: (() => void) | null = null

const segmenter = typeof Intl.Segmenter === 'function'
  ? new Intl.Segmenter('zh-CN', { granularity: 'grapheme' })
  : null

function splitGraphemes(value: string): string[] {
  if (!segmenter) return Array.from(value)
  return Array.from(segmenter.segment(value), (item) => item.segment)
}

function actionLabel(step: AgentExecutionStep): string {
  if (step.kind === 'model') return '规划并生成回复'
  return {
    Read: '读取项目文件',
    Write: '写入项目内容',
    Edit: '编辑项目内容',
    Glob: '检索项目文件',
    Grep: '搜索文件内容',
    Skill: '加载创作 Skill',
  }[step.name] || '调用创作工具'
}

const targetLabel = computed(() => {
  if (!props.active) return ''
  if (!props.steps.length) return '正在理解创作上下文'
  return activeStep.value ? `正在${actionLabel(activeStep.value)}` : ''
})

const currentIcon = computed(() => {
  const step = activeStep.value
  if (!step) return LoaderCircle
  if (step.kind === 'model') return BrainCircuit
  return {
    Read: FileText,
    Write: FilePenLine,
    Edit: FilePenLine,
    Glob: FileSearch,
    Grep: Search,
    Skill: Sparkles,
  }[step.name] || Wrench
})

function animateLabel(nextLabel: string): void {
  textTimeline?.kill()
  if (prefersReducedMotion()) {
    displayedLabel.value = nextLabel
    typing.value = false
    return
  }

  const currentSegments = splitGraphemes(displayedLabel.value)
  const nextSegments = splitGraphemes(nextLabel)
  const eraseProgress = { value: currentSegments.length }
  const typeProgress = { value: 0 }
  typing.value = true

  textTimeline = gsap.timeline({
    defaults: { ease: 'none', overwrite: 'auto' },
    onComplete: () => {
      displayedLabel.value = nextLabel
      typing.value = false
    },
  })

  if (currentSegments.length) {
    textTimeline.to(eraseProgress, {
      value: 0,
      duration: Math.min(0.24, Math.max(0.1, currentSegments.length * 0.012)),
      onUpdate: () => {
        displayedLabel.value = currentSegments.slice(0, Math.ceil(eraseProgress.value)).join('')
      },
    })
  }

  textTimeline.call(() => {
    displayedLabel.value = ''
  })

  if (nextSegments.length) {
    textTimeline.to(typeProgress, {
      value: nextSegments.length,
      duration: Math.min(0.72, Math.max(0.28, nextSegments.length * 0.036)),
      onUpdate: () => {
        displayedLabel.value = nextSegments.slice(0, Math.floor(typeProgress.value)).join('')
      },
    })
  }
}

onMounted(() => {
  stopLabelWatch = watch(targetLabel, animateLabel, { immediate: true })
})

onUnmounted(() => {
  stopLabelWatch?.()
  textTimeline?.kill()
})
</script>

<template>
  <Transition name="agent-execution-status">
    <div
      v-if="displayedLabel || targetLabel"
      class="agent-execution"
      role="status"
      aria-live="polite"
      aria-atomic="true"
    >
      <span class="agent-execution__glyph">
        <component
          :is="currentIcon"
          :class="{ spin: !activeStep }"
          :size="15"
        />
      </span>
      <span class="agent-execution__text">{{ displayedLabel }}</span>
      <i v-if="typing" class="agent-execution__caret" aria-hidden="true"></i>
    </div>
  </Transition>
</template>
