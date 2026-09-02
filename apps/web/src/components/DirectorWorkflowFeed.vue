<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import {
  Bot,
  Check,
  ChevronDown,
  CircleCheckBig,
  Clapperboard,
  Image,
  Layers3,
  LoaderCircle,
  MessageSquareWarning,
  RotateCcw,
  ShieldCheck,
  Sparkles,
  WandSparkles,
  XCircle,
} from 'lucide-vue-next'

import { api } from '@/lib/api'
import { useToastStore } from '@/stores/toast'
import type { DirectorChildRun, DirectorWorkflowDetail } from '@/types'

const props = defineProps<{
  projectId: string
  chapterId: string
}>()
const emit = defineEmits<{ changed: [] }>()
const toast = useToastStore()
const detail = ref<DirectorWorkflowDetail | null>(null)
const loading = ref(false)
const action = ref('')
const feedback = ref('')
let pollTimer: ReturnType<typeof setTimeout> | undefined

const stages = [
  { id: 'script', label: '剧本', icon: Clapperboard },
  { id: 'review', label: '审核', icon: ShieldCheck },
  { id: 'assets', label: '资产', icon: Image },
  { id: 'storyboard', label: '分镜', icon: Layers3 },
] as const

const stageIndex = computed(() => {
  const stage = detail.value?.workflow.stage ?? ''
  if (stage.includes('storyboard') || stage === 'ready_for_video') return 3
  if (stage.includes('asset') || stage === 'ready_for_asset_images') return 2
  if (stage.includes('review') || stage.includes('decision') || stage.includes('repair')) return 1
  return 0
})
const isBusy = computed(() => detail.value?.workflow.status === 'running')
const canStartStoryboard = computed(
  () => detail.value?.workflow.stage === 'ready_for_asset_images' && !isBusy.value,
)

watch(
  () => props.chapterId,
  () => void loadWorkflow(true),
  { immediate: true },
)

onUnmounted(() => {
  if (pollTimer) clearTimeout(pollTimer)
})

function schedulePoll(): void {
  if (pollTimer) clearTimeout(pollTimer)
  if (!detail.value || ['running'].includes(detail.value.workflow.status)) {
    pollTimer = setTimeout(() => void loadWorkflow(false), 1800)
  }
}

async function loadWorkflow(showLoading: boolean): Promise<void> {
  if (!props.chapterId) return
  if (showLoading) loading.value = true
  try {
    detail.value = await api<DirectorWorkflowDetail | null>(
      `/projects/${props.projectId}/chapters/${props.chapterId}/director-workflow`,
    )
    emit('changed')
  } catch (error) {
    toast.show('导演流程读取失败', {
      message: error instanceof Error ? error.message : undefined,
      tone: 'error',
    })
  } finally {
    loading.value = false
    schedulePoll()
  }
}

async function startWorkflow(): Promise<void> {
  action.value = 'start'
  try {
    detail.value = await api<DirectorWorkflowDetail>(
      `/projects/${props.projectId}/chapters/${props.chapterId}/director-workflow`,
      { method: 'POST', body: JSON.stringify({ instruction: '' }) },
    )
    toast.show('导演 Agent 已开始改编', { tone: 'success' })
    emit('changed')
  } catch (error) {
    toast.show('无法开始导演流程', {
      message: error instanceof Error ? error.message : undefined,
      tone: 'error',
    })
  } finally {
    action.value = ''
    schedulePoll()
  }
}

async function startStoryboard(): Promise<void> {
  if (!detail.value) return
  action.value = 'storyboard'
  try {
    detail.value = await api<DirectorWorkflowDetail>(
      `/projects/${props.projectId}/director-workflows/${detail.value.workflow.id}/storyboard`,
      { method: 'POST' },
    )
    toast.show('导演 Agent 正在检查资产并制作分镜', { tone: 'success' })
    emit('changed')
  } catch (error) {
    toast.show('分镜流程无法启动', {
      message: error instanceof Error ? error.message : undefined,
      tone: 'error',
    })
  } finally {
    action.value = ''
    schedulePoll()
  }
}

async function submitDecision(option: string): Promise<void> {
  const decision = detail.value?.pending_decision
  if (!decision || !detail.value) return
  if (option === 'provide_feedback' && !feedback.value.trim()) {
    toast.show('请先填写具体修改意见', { tone: 'info' })
    return
  }
  action.value = option
  try {
    detail.value = await api<DirectorWorkflowDetail>(
      `/projects/${props.projectId}/director-workflows/${detail.value.workflow.id}/decisions/${decision.id}`,
      {
        method: 'POST',
        body: JSON.stringify({ option, feedback: feedback.value.trim() }),
      },
    )
    feedback.value = ''
    emit('changed')
  } catch (error) {
    toast.show('审核选择提交失败', {
      message: error instanceof Error ? error.message : undefined,
      tone: 'error',
    })
  } finally {
    action.value = ''
    schedulePoll()
  }
}

function childIcon(child: DirectorChildRun) {
  if (child.status === 'succeeded') return CircleCheckBig
  if (child.status === 'failed' || child.status === 'cancelled') return XCircle
  if (child.kind.includes('review')) return ShieldCheck
  return Bot
}

function findings(child: DirectorChildRun): Array<Record<string, string>> {
  const rows = child.details.findings
  return Array.isArray(rows) ? rows as Array<Record<string, string>> : []
}
</script>

<template>
  <section class="director-agent-flow" aria-label="导演 Agent 执行流程">
    <header class="director-agent-flow__header">
      <div>
        <span><Sparkles :size="15" /></span>
        <div><strong>本章导演流程</strong><small>自动委派 · 可恢复</small></div>
      </div>
      <span v-if="detail" class="director-agent-flow__state" :data-status="detail.workflow.status">
        <LoaderCircle v-if="detail.workflow.status === 'running'" class="spin" :size="13" />
        <Check v-else-if="detail.workflow.status === 'completed'" :size="13" />
        <MessageSquareWarning v-else :size="13" />
        {{ detail.workflow.status === 'running' ? '执行中' : detail.workflow.status === 'waiting_user' ? '等你确认' : detail.workflow.status === 'completed' ? '本阶段完成' : '需要处理' }}
      </span>
    </header>

    <div v-if="loading" class="director-agent-flow__loading"><LoaderCircle class="spin" :size="18" />同步导演状态</div>

    <template v-else-if="detail">
      <nav class="director-agent-flow__steps" aria-label="导演流程阶段">
        <template v-for="(stage, index) in stages" :key="stage.id">
          <span :class="{ active: index === stageIndex, complete: index < stageIndex }">
            <component :is="stage.icon" :size="14" />{{ stage.label }}
          </span>
          <i v-if="index < stages.length - 1" :class="{ complete: index < stageIndex }"></i>
        </template>
      </nav>

      <div class="director-agent-flow__message">
        <span><WandSparkles :size="16" /></span>
        <p>{{ detail.workflow.last_message }}</p>
      </div>

      <div v-if="detail.child_runs.length" class="director-child-list">
        <details v-for="child in detail.child_runs" :key="child.id" class="director-child-run">
          <summary>
            <span class="director-child-run__icon" :data-status="child.status">
              <LoaderCircle v-if="child.status === 'queued' || child.status === 'running'" class="spin" :size="15" />
              <component :is="childIcon(child)" v-else :size="15" />
            </span>
            <span><strong>{{ child.title }}</strong><small>{{ child.status === 'queued' ? '已排队' : child.status === 'running' ? '正在执行' : child.status === 'succeeded' ? (child.summary || '执行完成') : (child.summary || '执行失败') }}</small></span>
            <b v-if="child.attempt > 1" class="tabular-nums">{{ child.attempt }}/{{ child.max_attempts }}</b>
            <ChevronDown :size="15" />
          </summary>
          <div class="director-child-run__details">
            <p v-if="child.summary">{{ child.summary }}</p>
            <ul v-if="findings(child).length">
              <li v-for="(finding, index) in findings(child)" :key="index" :data-severity="finding.severity">
                <strong>{{ finding.location || `问题 ${index + 1}` }}</strong>
                <span>{{ finding.issue }}</span>
                <small v-if="finding.suggestion">{{ finding.suggestion }}</small>
              </li>
            </ul>
            <dl>
              <div><dt>执行状态</dt><dd>{{ child.status }}</dd></div>
              <div><dt>尝试次数</dt><dd class="tabular-nums">{{ child.attempt }} / {{ child.max_attempts }}</dd></div>
            </dl>
          </div>
        </details>
      </div>

      <section v-if="detail.pending_decision" class="director-decision">
        <header><MessageSquareWarning :size="17" /><strong>需要你的决定</strong></header>
        <p>{{ detail.pending_decision.prompt }}</p>
        <textarea v-model="feedback" rows="3" placeholder="可选：补充你希望保留或调整的具体内容"></textarea>
        <div>
          <button
            v-for="option in detail.pending_decision.options"
            :key="option.value"
            type="button"
            :disabled="Boolean(action)"
            @click="submitDecision(option.value)"
          >
            <LoaderCircle v-if="action === option.value" class="spin" :size="15" />
            <RotateCcw v-else-if="option.value.includes('repair') || option.value === 'full_rewrite'" :size="15" />
            <Check v-else :size="15" />
            <span><strong>{{ option.label }}</strong><small>{{ option.description }}</small></span>
          </button>
        </div>
      </section>

      <button v-if="canStartStoryboard" class="director-agent-flow__primary" type="button" :disabled="Boolean(action)" @click="startStoryboard">
        <LoaderCircle v-if="action === 'storyboard'" class="spin" :size="17" />
        <Layers3 v-else :size="17" />
        <span><strong>让导演 Agent 制作分镜</strong><small>先自动验证并补全所有必需资产</small></span>
      </button>
    </template>

    <div v-else class="director-agent-flow__empty">
      <span><Clapperboard :size="21" /></span>
      <strong>从章节原文开始</strong>
      <p>导演 Agent 会读取项目记忆、剧本规则和导演 Skills，自动完成改编、审核与资产提取。</p>
      <button type="button" :disabled="action === 'start'" @click="startWorkflow">
        <LoaderCircle v-if="action === 'start'" class="spin" :size="16" />
        <Sparkles v-else :size="16" />开始创作本章
      </button>
    </div>
  </section>
</template>
