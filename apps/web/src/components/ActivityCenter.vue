<script setup lang="ts">
import { computed, ref } from 'vue'
import {
  ArrowLeft, Bell, Bot, Boxes, CheckCheck, CircleCheck, CircleX, Clapperboard, Clock3,
  FileOutput, FileSearch, Image, LayoutTemplate, LoaderCircle, MessageSquareText,
  Music2, RefreshCcw, RotateCcw, Sparkles, Video, WandSparkles, X,
} from 'lucide-vue-next'
import { PopoverContent, PopoverPortal, PopoverRoot, PopoverTrigger } from 'reka-ui'

import { useActivityStore } from '@/stores/activity'
import { useToastStore } from '@/stores/toast'
import type { AITask, NotificationItem, TaskEvent, TaskStatus } from '@/types'

type TaskFilter = 'all' | 'active' | 'failed' | 'completed'

const activity = useActivityStore()
const toast = useToastStore()
const open = ref(false)
const tab = ref<'tasks' | 'notifications'>('tasks')
const filter = ref<TaskFilter>('all')
const actionId = ref<string | null>(null)
const selectedTask = ref<AITask | null>(null)
const selectedEvents = ref<TaskEvent[]>([])
const detailLoading = ref(false)

function closePanel(): void {
  open.value = false
  selectedTask.value = null
}

const badgeCount = computed(() => activity.unreadCount + activity.activeCount)
const filteredTasks = computed(() => activity.tasks.filter((task) => {
  if (filter.value === 'active') return task.status === 'queued' || task.status === 'running'
  if (filter.value === 'failed') return task.status === 'failed' || task.status === 'cancelled'
  if (filter.value === 'completed') return task.status === 'succeeded'
  return true
}))

const statusCopy: Record<TaskStatus, { label: string; icon: typeof Clock3 }> = {
  queued: { label: '排队中', icon: Clock3 },
  running: { label: '进行中', icon: LoaderCircle },
  succeeded: { label: '已完成', icon: CircleCheck },
  failed: { label: '失败', icon: CircleX },
  cancelled: { label: '已取消', icon: X },
}

const taskMeta: Record<string, { title: string; category: string; icon: typeof Sparkles }> = {
  agent_chat_run: { title: 'Agent 创作回复', category: 'Agent', icon: Bot },
  project_cover_generation: { title: '项目封面生成', category: '图片', icon: Image },
  chapter_analysis_generation: { title: '章节内容分析', category: '文本', icon: FileSearch },
  chapter_script_generation: { title: 'AI 剧本生成', category: '剧本', icon: Clapperboard },
  chapter_asset_extraction: { title: '剧本资产提取', category: '资产', icon: Boxes },
  asset_prompt_generation: { title: '资产提示词生成', category: '资产', icon: WandSparkles },
  asset_image_generation: { title: '资产图片生成', category: '图片', icon: Image },
  chapter_storyboard_generation: { title: '章节分镜生成', category: '分镜', icon: LayoutTemplate },
  shot_video_prompt_generation: { title: '镜头视频提示词', category: '视频', icon: WandSparkles },
  shot_video_generation: { title: '镜头视频生成', category: '视频', icon: Video },
  chapter_dialogue_extraction: { title: '章节台词提取', category: '台词', icon: MessageSquareText },
  dialogue_tts_generation: { title: '角色台词配音', category: '音频', icon: Music2 },
  chapter_composition_render: { title: '章节成片渲染', category: '成片', icon: FileOutput },
}

const filters: Array<{ value: TaskFilter; label: string }> = [
  { value: 'all', label: '全部' },
  { value: 'active', label: '处理中' },
  { value: 'failed', label: '失败' },
  { value: 'completed', label: '完成' },
]

function metaFor(task: AITask) {
  if (task.task_type === 'agent_chat_run' && task.request_payload.scope === 'personal') {
    return {
      image: { title: '个人图片生成', category: '图片', icon: Image },
      video: { title: '个人视频生成', category: '视频', icon: Video },
      skill: { title: '个人 Skill 创作', category: 'Skill', icon: WandSparkles },
    }[String(task.request_payload.mode || '')]
      || { title: '个人 Agent 回复', category: 'Agent', icon: Bot }
  }
  return taskMeta[task.task_type] || { title: 'AI 生成任务', category: 'AI', icon: Sparkles }
}

function serverDate(value: string): Date {
  const hasTimezone = /(?:z|[+-]\d{2}:?\d{2})$/i.test(value)
  return new Date(hasTimezone ? value : `${value}Z`)
}

function relativeTime(value: string): string {
  const seconds = Math.max(0, Math.round((Date.now() - serverDate(value).getTime()) / 1000))
  if (seconds < 60) return '刚刚'
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`
  return `${Math.floor(seconds / 86400)} 天前`
}

function exactTime(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).format(serverDate(value))
}

function durationLabel(task: AITask): string {
  if (!task.started_at) return '尚未开始'
  const end = task.completed_at ? serverDate(task.completed_at).getTime() : Date.now()
  const seconds = Math.max(0, Math.round((end - serverDate(task.started_at).getTime()) / 1000))
  if (seconds < 60) return `${seconds} 秒`
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  return `${hours} 小时 ${minutes} 分`
}

function shortId(value: string | null): string {
  return value ? value.slice(0, 8).toUpperCase() : '系统任务'
}

async function openTask(task: AITask): Promise<void> {
  selectedTask.value = task
  selectedEvents.value = []
  detailLoading.value = true
  try {
    selectedEvents.value = await activity.taskEvents(task.id)
  } catch (error) {
    toast.show('任务轨迹加载失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    detailLoading.value = false
  }
}

async function openNotification(item: NotificationItem): Promise<void> {
  await activity.markRead(item.id)
  if (!item.task_id) return
  const task = activity.tasks.find((row) => row.id === item.task_id)
  if (task) await openTask(task)
}

async function taskAction(task: AITask, action: 'retry' | 'cancel'): Promise<void> {
  actionId.value = task.id
  try {
    if (action === 'retry') await activity.retryTask(task.id)
    else await activity.cancelTask(task.id)
    const updated = activity.tasks.find((item) => item.id === task.id)
    if (selectedTask.value?.id === task.id && updated) await openTask(updated)
    toast.show(action === 'retry' ? '任务已重新排队' : '任务已取消', { tone: 'success' })
  } catch (error) {
    toast.show('任务操作失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    actionId.value = null
  }
}
</script>

<template>
  <PopoverRoot v-model:open="open">
    <PopoverTrigger class="icon-button activity-trigger" type="button" title="任务与通知" aria-label="任务与通知">
      <Bell :size="18" />
      <span v-if="badgeCount" class="activity-trigger__badge">{{ Math.min(badgeCount, 99) }}</span>
    </PopoverTrigger>
    <PopoverPortal>
      <PopoverContent class="activity-panel" :side-offset="10" align="end">
        <Transition name="activity-view" mode="out-in">
          <section v-if="selectedTask" :key="`detail-${selectedTask.id}`" class="activity-detail">
            <header class="activity-panel__header activity-detail__header">
              <button class="icon-button icon-button--small" type="button" title="返回任务列表" @click="selectedTask = null"><ArrowLeft :size="16" /></button>
              <div><strong>任务详情</strong><span>持久化执行轨迹</span></div>
              <div class="activity-panel__header-actions">
                <button class="icon-button icon-button--small" type="button" title="刷新任务详情" @click="openTask(selectedTask)"><RefreshCcw :class="{ spin: detailLoading }" :size="15" /></button>
                <button class="icon-button icon-button--small activity-panel__close" type="button" title="关闭动态中心" aria-label="关闭动态中心" @click="closePanel"><X :size="17" /></button>
              </div>
            </header>

            <div class="task-detail-scroll">
              <section class="task-detail-hero" :data-status="selectedTask.status">
                <span class="task-detail-hero__icon"><component :is="metaFor(selectedTask).icon" :size="22" /></span>
                <div><span>{{ metaFor(selectedTask).category }} · {{ shortId(selectedTask.id) }}</span><h3>{{ metaFor(selectedTask).title }}</h3><p>{{ selectedTask.latest_message || selectedTask.error_message || statusCopy[selectedTask.status].label }}</p></div>
                <strong>{{ selectedTask.progress }}%</strong>
                <span class="task-detail-progress"><i :style="{ width: `${selectedTask.progress}%` }"></i></span>
              </section>

              <dl class="task-detail-metrics">
                <div><dt>当前状态</dt><dd :data-status="selectedTask.status"><component :is="statusCopy[selectedTask.status].icon" :size="14" />{{ statusCopy[selectedTask.status].label }}</dd></div>
                <div><dt>积分记录</dt><dd>{{ selectedTask.cost }}<small v-if="selectedTask.result_payload?.credit_refunded">已退回</small></dd></div>
                <div><dt>项目标识</dt><dd>{{ shortId(selectedTask.project_id) }}</dd></div>
                <div><dt>模型标识</dt><dd>{{ shortId(selectedTask.model_id) }}</dd></div>
              </dl>

              <dl class="task-detail-timing">
                <div><dt>创建任务</dt><dd>{{ exactTime(selectedTask.created_at) }}</dd></div>
                <div><dt>开始执行</dt><dd>{{ selectedTask.started_at ? exactTime(selectedTask.started_at) : '等待 Worker 领取' }}</dd></div>
                <div><dt>结束执行</dt><dd>{{ selectedTask.completed_at ? exactTime(selectedTask.completed_at) : '尚未结束' }}</dd></div>
                <div><dt>本次耗时</dt><dd>{{ durationLabel(selectedTask) }}</dd></div>
              </dl>

              <section v-if="selectedTask.error_message" class="task-detail-error">
                <CircleX :size="17" /><div><strong>{{ selectedTask.status === 'cancelled' ? '处理说明' : '失败原因' }}</strong><p>{{ selectedTask.error_message }}</p></div>
              </section>

              <section class="task-timeline">
                <header><div><strong>执行轨迹</strong><span>{{ selectedEvents.length }} 条持久化记录</span></div><Clock3 :size="16" /></header>
                <div v-if="detailLoading" class="task-timeline__loading"><LoaderCircle class="spin" :size="18" />正在同步任务轨迹</div>
                <ol v-else-if="selectedEvents.length">
                  <li v-for="(event, index) in [...selectedEvents].reverse()" :key="event.id" v-motion="{ preset: 'row', index }" :data-status="event.status">
                    <span><component :is="statusCopy[event.status].icon" :size="14" /></span>
                    <div><strong>{{ event.message }}</strong><p>{{ statusCopy[event.status].label }} · {{ event.progress }}%</p></div>
                    <time>{{ exactTime(event.created_at) }}</time>
                  </li>
                </ol>
                <div v-else class="task-timeline__loading">暂无执行记录</div>
              </section>
            </div>

            <footer v-if="['queued', 'failed', 'cancelled'].includes(selectedTask.status)" class="task-detail-actions">
              <button v-if="selectedTask.status === 'queued'" type="button" :disabled="actionId === selectedTask.id" @click="taskAction(selectedTask, 'cancel')"><X :size="15" />取消任务</button>
              <button v-else class="primary" type="button" :disabled="actionId === selectedTask.id" @click="taskAction(selectedTask, 'retry')"><RotateCcw :size="15" />重新执行</button>
            </footer>
          </section>

          <section v-else key="overview" class="activity-overview">
            <header class="activity-panel__header">
              <div>
                <strong>动态中心</strong>
                <span class="activity-panel__subtitle">
                  {{ activity.activeCount ? `${activity.activeCount} 个任务正在处理` : '所有任务均已同步' }}
                  <i :data-state="activity.streamState"></i>
                  {{ activity.streamState === 'live' ? '实时同步' : activity.streamState === 'connecting' ? '正在连接' : '定时同步' }}
                </span>
              </div>
              <div class="activity-panel__header-actions">
                <button class="icon-button icon-button--small" type="button" title="刷新" @click="activity.refresh"><RefreshCcw :class="{ spin: activity.loading }" :size="15" /></button>
                <button class="icon-button icon-button--small activity-panel__close" type="button" title="关闭动态中心" aria-label="关闭动态中心" @click="closePanel"><X :size="17" /></button>
              </div>
            </header>

            <div class="activity-tabs" role="tablist">
              <button :class="{ active: tab === 'tasks' }" type="button" @click="tab = 'tasks'"><WandSparkles :size="15" />生成任务 <span>{{ activity.tasks.length }}</span></button>
              <button :class="{ active: tab === 'notifications' }" type="button" @click="tab = 'notifications'"><Bell :size="15" />消息通知 <span>{{ activity.unreadCount }}</span></button>
            </div>

            <section v-if="tab === 'tasks'" class="activity-task-view">
              <div class="activity-filters" aria-label="任务状态筛选">
                <button v-for="item in filters" :key="item.value" :class="{ active: filter === item.value }" type="button" @click="filter = item.value">{{ item.label }}</button>
              </div>
              <div class="activity-list">
                <article v-for="(task, index) in filteredTasks" :key="task.id" v-motion="{ preset: 'row', index }" class="task-activity" :data-status="task.status">
                  <button class="task-activity__open" type="button" @click="openTask(task)">
                    <span class="task-activity__icon"><component :is="metaFor(task).icon" :size="18" /></span>
                    <span class="task-activity__body">
                      <span class="task-activity__title"><strong>{{ metaFor(task).title }}</strong><time>{{ relativeTime(task.latest_event_at || task.updated_at) }}</time></span>
                      <span class="task-activity__message">{{ task.latest_message || task.error_message || statusCopy[task.status].label }}</span>
                      <span v-if="task.status === 'queued' || task.status === 'running'" class="task-progress"><i :style="{ width: `${task.progress}%` }"></i></span>
                    </span>
                    <span class="task-activity__status"><component :is="statusCopy[task.status].icon" :class="{ spin: task.status === 'running' }" :size="14" />{{ task.status === 'running' ? `${task.progress}%` : statusCopy[task.status].label }}</span>
                  </button>
                  <button v-if="task.status === 'failed' || task.status === 'cancelled'" class="task-activity__action" type="button" :disabled="actionId === task.id" @click="taskAction(task, 'retry')"><RotateCcw :size="14" />重试</button>
                  <button v-else-if="task.status === 'queued'" class="task-activity__action" type="button" :disabled="actionId === task.id" @click="taskAction(task, 'cancel')"><X :size="14" />取消</button>
                </article>
                <div v-if="!filteredTasks.length" class="activity-empty"><Clock3 :size="24" /><span>当前筛选下暂无任务</span></div>
              </div>
            </section>

            <div v-else class="activity-list notification-list">
              <button v-for="(item, index) in activity.notifications" :key="item.id" v-motion="{ preset: 'row', index }" class="notification-activity" :class="{ unread: !item.is_read }" type="button" @click="openNotification(item)">
                <span class="notification-activity__icon"><Bell :size="15" /></span>
                <span><strong>{{ item.title }}</strong><p>{{ item.message }}</p><time>{{ relativeTime(item.created_at) }}</time></span>
                <span v-if="!item.is_read" class="notification-activity__dot"></span>
              </button>
              <div v-if="!activity.notifications.length" class="activity-empty"><Bell :size="24" /><span>暂无消息通知</span></div>
            </div>

            <footer v-if="tab === 'notifications' && activity.unreadCount" class="activity-panel__footer">
              <button type="button" @click="activity.markAllRead"><CheckCheck :size="15" />全部标为已读</button>
            </footer>
          </section>
        </Transition>
      </PopoverContent>
    </PopoverPortal>
  </PopoverRoot>
</template>
