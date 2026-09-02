<script setup lang="ts">
import { computed } from 'vue'
import {
  BrainCircuit,
  Check,
  ChevronDown,
  FilePenLine,
  FileSearch,
  FileText,
  ListChecks,
  LoaderCircle,
  Search,
  Sparkles,
  TriangleAlert,
  Wrench,
} from 'lucide-vue-next'

import type { AgentExecutionStep } from '@/types'

const props = withDefaults(defineProps<{
  steps: AgentExecutionStep[]
  active?: boolean
  expanded?: boolean
  outputLength?: number
}>(), {
  active: false,
  expanded: false,
  outputLength: 0,
})

defineEmits<{ toggle: [] }>()

const completedCount = computed(() => props.steps.filter((step) => step.status === 'succeeded').length)
const failedCount = computed(() => props.steps.filter((step) => step.status === 'failed').length)
const runningCount = computed(() => props.steps.filter((step) => step.status === 'running').length)
const currentStep = computed(() => [...props.steps].reverse().find((step) => step.status === 'running'))
const summary = computed(() => {
  if (currentStep.value) return stepLabel(currentStep.value)
  if (props.active && !props.steps.length) return '正在理解项目上下文'
  if (failedCount.value) return `${failedCount.value} 个步骤需要关注`
  return props.steps.length ? '执行步骤已完成' : '已完成本轮生成'
})
const detail = computed(() => {
  const parts: string[] = []
  if (completedCount.value) parts.push(`${completedCount.value} 已完成`)
  if (runningCount.value) parts.push(`${runningCount.value} 进行中`)
  if (failedCount.value) parts.push(`${failedCount.value} 失败`)
  return parts.join(' · ') || (props.active ? '准备执行' : '无额外工具调用')
})

function stepLabel(step: AgentExecutionStep): string {
  if (step.kind === 'model') return '模型规划与生成'
  return {
    Read: '读取项目文件',
    Write: '写入项目内容',
    Edit: '编辑项目内容',
    Glob: '检索项目文件',
    Grep: '搜索文件内容',
    Skill: '加载创作技能',
  }[step.name] || '执行项目工具'
}

function stepStatusLabel(step: AgentExecutionStep): string {
  if (step.status === 'running') return '进行中'
  if (step.status === 'failed') return '失败'
  return '已完成'
}

function stepIcon(step: AgentExecutionStep) {
  if (step.kind === 'model') return BrainCircuit
  return {
    Read: FileText,
    Write: FilePenLine,
    Edit: FilePenLine,
    Glob: FileSearch,
    Grep: Search,
    Skill: Sparkles,
  }[step.name] || Wrench
}
</script>

<template>
  <section class="agent-execution" :class="{ 'is-active': active, 'has-failure': failedCount }">
    <button
      class="agent-execution__trigger"
      type="button"
      :aria-expanded="expanded"
      title="查看 Agent 的模型与工具执行状态"
      @click="$emit('toggle')"
    >
      <span class="agent-execution__icon">
        <LoaderCircle v-if="active" class="spin" :size="16" />
        <ListChecks v-else :size="16" />
      </span>
      <span class="agent-execution__summary">
        <strong>{{ summary }}</strong>
        <small>{{ detail }}</small>
      </span>
      <b v-if="outputLength">{{ outputLength }} 字</b>
      <ChevronDown class="agent-execution__chevron" :class="{ active: expanded }" :size="15" />
    </button>

    <div class="agent-execution__reveal" :class="{ active: expanded }">
      <div>
        <ol v-if="steps.length">
          <li v-for="step in steps" :key="step.id" :class="`is-${step.status}`">
            <span class="agent-execution__step-icon">
              <component :is="stepIcon(step)" :size="14" />
            </span>
            <span>
              <strong>{{ stepLabel(step) }}</strong>
              <small>{{ step.kind === 'model' ? 'Agent 推理阶段' : '安全工具调用' }}</small>
            </span>
            <span class="agent-execution__step-state">
              <LoaderCircle v-if="step.status === 'running'" class="spin" :size="14" />
              <TriangleAlert v-else-if="step.status === 'failed'" :size="14" />
              <Check v-else :size="14" />
              {{ stepStatusLabel(step) }}
            </span>
          </li>
        </ol>
        <div v-else class="agent-execution__preparing">
          <LoaderCircle v-if="active" class="spin" :size="14" />
          <Check v-else :size="14" />
          <span>{{ active ? '正在准备模型、项目资料与技能上下文' : '本轮未调用额外工具' }}</span>
        </div>
      </div>
    </div>
  </section>
</template>
