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
const historyExpanded = ref(false)
const visibleChildren = computed(() => historyExpanded.value
  ? props.detail.child_runs
  : props.detail.child_runs.filter((child) => ['queued', 'running', 'failed'].includes(child.status)).slice(-3))

const feedbackPlaceholder = computed(() => (
  props.detail.pending_decision?.decision_type === 'storyboard_review'
    ? '可选：写清镜号与要改的字段，例如「镜头 7 的台词太长」，平台只修这一镜'
    : '可选：补充希望保留或调整的内容'
))

const activeChild = computed(() => [...props.detail.child_runs].reverse().find((child) => (
  child.status === 'queued' || child.status === 'running'
)))
const completedCount = computed(() => props.detail.child_runs.filter((child) => child.status === 'succeeded').length)
const statusLabel = computed(() => {
  if (props.detail.workflow.status === 'running') return activeChild.value?.summary || activeChild.value?.title || '正在推进导演流程'
  if (props.detail.pending_decision?.decision_type === 'jev_control') return '判断暂未完成，等待确认后继续'
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
        <small>当前章节 · {{ detail.workflow.automation_mode ? '全自动制作' : '制作进度' }}</small>
        <strong>{{ statusLabel }}</strong>
      </span>
      <span v-if="detail.child_runs.length" class="director-subtask-card__count tabular-nums">
        {{ completedCount }}/{{ detail.child_runs.length }}
      </span>
    </header>

    <p v-if="detail.workflow.last_message && detail.workflow.last_message !== statusLabel" class="director-subtask-card__message">{{ detail.workflow.last_message }}</p>

    <button v-if="detail.child_runs.length" class="director-subtask-card__history" type="button" :aria-expanded="historyExpanded" @click="historyExpanded = !historyExpanded">
      <span>{{ historyExpanded ? '收起执行记录' : `查看执行记录 · ${detail.child_runs.length} 项` }}</span><ChevronDown :size="14" />
    </button>

    <div v-if="detail.pending_decision" class="director-subtask-card__decision">
      <div><MessageSquareWarning :size="15" /><span>{{ detail.pending_decision.prompt }}</span></div>
      <textarea v-if="detail.pending_decision.decision_type !== 'jev_control'" v-model="feedback" rows="2" :placeholder="feedbackPlaceholder"></textarea>
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
        v-for="child in visibleChildren"
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
            <small>{{ childStateLabel(child) }}<template v-if="child.attempt > 1"> · 第 {{ child.attempt }} 次尝试</template></small>
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

<style scoped>
.director-subtask-card {
  box-sizing: border-box;
  width: calc(100% - 24px);
  max-width: 620px;
  margin: 8px auto 16px;
  border: 1px solid var(--glass-edge, var(--line));
  border-radius: 16px;
  color: var(--ink);
  background: var(--glass-inset, var(--surface));
  box-shadow: none;
}
.director-subtask-card__header { grid-template-columns: 32px minmax(0, 1fr) auto; gap: 8px; padding: 12px; }
.director-subtask-card__status-icon, .director-subtask-card__step-icon {
  width: 30px; height: 30px; border-radius: 10px;
  color: var(--brand); background: color-mix(in srgb, var(--brand) 10%, transparent); box-shadow: none;
}
.director-subtask-card__title small { color: var(--ink-secondary); font-size: 11px; font-weight: 500; }
.director-subtask-card__title strong { white-space: normal; overflow-wrap: anywhere; font-size: 13px; line-height: 1.6; }
.director-subtask-card__count { background: transparent; box-shadow: none; color: var(--ink-secondary); }
.director-subtask-card__message { padding: 0 12px 8px; font-size: 12px; line-height: 1.7; overflow-wrap: anywhere; }
.director-subtask-card__history { display: flex; align-items: center; justify-content: space-between; gap: 8px;
  width: 100%; min-height: 40px; padding: 8px 12px; border: 0; background: transparent; color: var(--ink-secondary); cursor: pointer; font-size: 12px; }
.director-child-agent-stack { max-height: 320px; overflow: auto; overscroll-behavior: contain; gap: 6px; }
.director-child-agent-card, .director-child-agent-card:hover {
  background: var(--glass-inset, var(--surface)); border-radius: 10px;
  box-shadow: inset 0 0 0 1px var(--glass-edge, var(--line)); transform: none;
}
.director-child-agent-card summary { min-height: 48px; padding: 8px; }
.director-child-agent-card summary strong, .director-child-agent-card__body { color: var(--ink); overflow-wrap: anywhere; }
.director-child-agent-card summary small, .director-child-agent-card__body p { color: var(--ink-secondary); font-size: 12px; }
.director-subtask-card__decision { margin: 0 10px 10px; padding: 10px; background: var(--glass-inset, var(--surface)); border: 1px solid var(--glass-edge, var(--line)); border-radius: 10px; }
.director-subtask-card__decision textarea, .director-subtask-card__options button {
  color: var(--ink); background: var(--glass-inset, var(--surface)); border-color: var(--glass-edge, var(--line)); min-height: 40px;
}
.director-subtask-card__options { grid-template-columns: minmax(0, 1fr); }
.director-subtask-card__options button small { color: var(--ink-secondary); }
</style>
