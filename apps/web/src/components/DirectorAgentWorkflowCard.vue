<script setup lang="ts">
import { computed, ref } from 'vue'
import {
  Check,
  ChevronDown,
  CircleCheckBig,
  LoaderCircle,
  MessageSquareWarning,
  RotateCcw,
  ShieldCheck,
  XCircle,
} from 'lucide-vue-next'

import type { DirectorChildRun, DirectorWorkflowDetail } from '@/types'

const props = withDefaults(defineProps<{
  detail: DirectorWorkflowDetail
  submittingOption?: string
}>(), {
  submittingOption: '',
})

defineEmits<{
  decide: [option: string, feedback: string]
}>()

const feedback = ref('')

const activeChild = computed(() => [...props.detail.child_runs].reverse().find((child) => (
  child.status === 'queued' || child.status === 'running'
)))
const completedCount = computed(() => props.detail.child_runs.filter((child) => child.status === 'succeeded').length)
const statusLabel = computed(() => {
  if (props.detail.workflow.status === 'running') return activeChild.value?.summary || activeChild.value?.title || '正在推进导演流程'
  if (props.detail.pending_decision) return '审核发现问题，需要你的决定'
  if (props.detail.workflow.status === 'completed') return '本阶段已经完成'
  if (props.detail.workflow.status === 'failed') return props.detail.workflow.last_error || '子任务执行失败'
  return props.detail.workflow.last_message
})

function childStateLabel(child: DirectorChildRun): string {
  if (child.status === 'queued') return '排队中'
  if (child.status === 'running') return '执行中'
  if (child.status === 'succeeded') return '已完成'
  if (child.status === 'cancelled') return '已取消'
  return '失败'
}

function findings(child: DirectorChildRun): Array<Record<string, string>> {
  return Array.isArray(child.details.findings)
    ? child.details.findings as Array<Record<string, string>>
    : []
}
</script>

<template>
  <article class="director-subtask-card" :data-status="detail.workflow.status">
    <header class="director-subtask-card__header">
      <span class="director-subtask-card__status-icon">
        <LoaderCircle v-if="detail.workflow.status === 'running'" class="spin" :size="17" />
        <MessageSquareWarning v-else-if="detail.pending_decision" :size="17" />
        <CircleCheckBig v-else-if="detail.workflow.status === 'completed'" :size="17" />
        <XCircle v-else-if="detail.workflow.status === 'failed'" :size="17" />
        <ShieldCheck v-else :size="17" />
      </span>
      <span class="director-subtask-card__title">
        <small>导演 Agent · 内部子任务</small>
        <strong>{{ statusLabel }}</strong>
      </span>
      <span v-if="detail.child_runs.length" class="director-subtask-card__count tabular-nums">
        {{ completedCount }}/{{ detail.child_runs.length }}
      </span>
    </header>

    <p class="director-subtask-card__message">{{ detail.workflow.last_message }}</p>

    <div v-if="detail.pending_decision" class="director-subtask-card__decision">
      <div><MessageSquareWarning :size="15" /><span>{{ detail.pending_decision.prompt }}</span></div>
      <textarea v-model="feedback" rows="2" placeholder="可选：补充希望保留或调整的内容"></textarea>
      <div class="director-subtask-card__options">
        <button
          v-for="option in detail.pending_decision.options"
          :key="option.value"
          type="button"
          :disabled="Boolean(submittingOption)"
          @click="$emit('decide', option.value, feedback.trim())"
        >
          <LoaderCircle v-if="submittingOption === option.value" class="spin" :size="14" />
          <RotateCcw v-else-if="option.value === 'partial_repair' || option.value === 'full_rewrite'" :size="14" />
          <Check v-else :size="14" />
          <span><strong>{{ option.label }}</strong><small>{{ option.description }}</small></span>
        </button>
      </div>
    </div>

    <section v-if="detail.child_runs.length" class="director-child-agent-stack">
      <details
        v-for="child in detail.child_runs"
        :key="child.id"
        class="director-child-agent-card"
        :data-status="child.status"
      >
        <summary>
          <span class="director-subtask-card__step-icon">
            <LoaderCircle v-if="child.status === 'queued' || child.status === 'running'" class="spin" :size="14" />
            <CircleCheckBig v-else-if="child.status === 'succeeded'" :size="14" />
            <XCircle v-else :size="14" />
          </span>
          <span>
            <small>子智能体 {{ child.attempt }}/{{ child.max_attempts }} · {{ childStateLabel(child) }}</small>
            <strong>{{ child.title }}</strong>
          </span>
          <ChevronDown :size="14" />
        </summary>
        <div class="director-child-agent-card__body">
          <p>{{ child.summary || '任务已派发，等待返回结果' }}</p>
          <template v-if="findings(child).length">
            <em v-for="(finding, index) in findings(child)" :key="index">
              {{ finding.location || `问题 ${index + 1}` }}：{{ finding.issue }}
            </em>
          </template>
        </div>
      </details>
    </section>
  </article>
</template>
