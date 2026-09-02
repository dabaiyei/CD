import { defineStore } from 'pinia'

import { api, apiEventStream } from '@/lib/api'
import type {
  AITask,
  AgentExecutionStep,
  NotificationItem,
  NotificationPage,
  TaskEvent,
  TaskPage,
  TaskStatus,
} from '@/types'

const RECONCILE_INTERVAL_MS = 30_000
const STREAM_RECONNECT_MS = 3_000

interface ActivityEvent {
  type: string
  transport?: 'redis' | 'polling'
  task_id?: string
  project_id?: string | null
  status?: TaskStatus
  progress?: number
  message?: string
  created_at?: string
  session_id?: string
  event?: 'text.delta' | 'phase' | 'model.start' | 'model.end' | 'tool.start' | 'tool.end' | 'snapshot'
  delta?: string
  text?: string
  phase?: AgentStreamState['phase']
  tool_name?: string
  tool_state?: string
  step_id?: string
  step_state?: string
  execution_steps?: Array<{
    id?: string
    kind?: string
    name?: string
    status?: string
    started_at?: string
    completed_at?: string
  }>
}

export interface AgentStreamState {
  taskId: string
  sessionId: string
  text: string
  phase: 'queued' | 'thinking' | 'tool' | 'writing' | 'finalizing'
  toolName: string
  toolState: string
  steps: AgentExecutionStep[]
  updatedAt: string
}

function normalizeStepStatus(value?: string): AgentExecutionStep['status'] {
  const state = (value || '').toLowerCase()
  if (['error', 'failed', 'failure', 'cancelled', 'canceled'].includes(state)) return 'failed'
  if (['running', 'started', 'pending'].includes(state)) return 'running'
  return 'succeeded'
}

function upsertExecutionStep(
  stream: AgentStreamState,
  step: AgentExecutionStep,
): void {
  const index = stream.steps.findIndex((item) => item.id === step.id)
  if (index >= 0) stream.steps.splice(index, 1, { ...stream.steps[index]!, ...step })
  else stream.steps.push(step)
}

let streamController: AbortController | undefined
let refreshTimer: ReturnType<typeof setTimeout> | undefined

function reconnectDelay(signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, STREAM_RECONNECT_MS)
    signal.addEventListener('abort', () => {
      clearTimeout(timer)
      resolve()
    }, { once: true })
  })
}

export const useActivityStore = defineStore('activity', {
  state: () => ({
    tasks: [] as AITask[],
    notifications: [] as NotificationItem[],
    unreadCount: 0,
    loading: false,
    streamState: 'connecting' as 'connecting' | 'live' | 'degraded' | 'offline',
    agentStreams: {} as Record<string, AgentStreamState>,
    timer: undefined as ReturnType<typeof setInterval> | undefined,
  }),
  getters: {
    activeCount: (state) => state.tasks.filter((task) => ['queued', 'running'].includes(task.status)).length,
  },
  actions: {
    upsertTask(task: AITask): void {
      const index = this.tasks.findIndex((item) => item.id === task.id)
      if (index >= 0) this.tasks.splice(index, 1, { ...this.tasks[index]!, ...task })
      else this.tasks.unshift(task)
    },
    beginAgentStream(taskId: string, sessionId: string): void {
      const current = this.agentStreams[taskId]
      if (current) {
        current.sessionId = sessionId || current.sessionId
        return
      }
      this.agentStreams[taskId] = {
        taskId,
        sessionId,
        text: '',
        phase: 'queued',
        toolName: '',
        toolState: '',
        steps: [],
        updatedAt: new Date().toISOString(),
      }
    },
    reconcileAgentStreamText(taskId: string, text: string): void {
      const stream = this.agentStreams[taskId]
      if (!stream) return
      stream.text = text
      stream.phase = 'finalizing'
      stream.updatedAt = new Date().toISOString()
    },
    clearAgentStream(taskId: string): void {
      delete this.agentStreams[taskId]
    },
    async refresh(): Promise<void> {
      if (this.loading) return
      this.loading = true
      try {
        const [taskPage, notificationPage] = await Promise.all([
          api<TaskPage>('/tasks?limit=30'),
          api<NotificationPage>('/notifications?limit=30'),
        ])
        this.tasks = taskPage.items
        this.notifications = notificationPage.items
        this.unreadCount = notificationPage.unread_count
      } catch {
        // Authentication can change while the shell is mounting or unmounting.
      } finally {
        this.loading = false
      }
    },
    start(): void {
      if (this.timer) return
      this.streamState = 'connecting'
      void this.refresh()
      this.timer = setInterval(() => void this.refresh(), RECONCILE_INTERVAL_MS)
      streamController = new AbortController()
      void this.connectStream(streamController.signal)
    },
    stop(): void {
      if (this.timer) clearInterval(this.timer)
      this.timer = undefined
      streamController?.abort()
      streamController = undefined
      this.streamState = 'offline'
      this.agentStreams = {}
      if (refreshTimer) clearTimeout(refreshTimer)
      refreshTimer = undefined
    },
    scheduleRefresh(): void {
      if (refreshTimer) return
      refreshTimer = setTimeout(() => {
        refreshTimer = undefined
        void this.refresh()
      }, 120)
    },
    applyActivityEvent(event: ActivityEvent): void {
      if (event.type === 'stream.ready') {
        this.streamState = event.transport === 'redis' ? 'live' : 'degraded'
        return
      }
      if (event.type === 'stream.degraded') {
        this.streamState = 'degraded'
        return
      }
      if (event.type === 'agent.stream' && event.task_id && event.session_id && event.event) {
        this.beginAgentStream(event.task_id, event.session_id)
        const stream = this.agentStreams[event.task_id]
        if (!stream) return
        if (event.event === 'text.delta' && event.delta) {
          stream.text += event.delta
          stream.phase = 'writing'
        } else if (event.event === 'snapshot') {
          stream.text = event.text || ''
          stream.phase = event.phase || (stream.text ? 'writing' : 'thinking')
          stream.toolName = event.tool_name || ''
          stream.toolState = event.tool_state || ''
          stream.steps = (event.execution_steps || []).flatMap((step) => {
            if (!step.id || !['model', 'tool'].includes(step.kind || '')) return []
            return [{
              id: step.id,
              kind: step.kind as AgentExecutionStep['kind'],
              name: step.name || (step.kind === 'model' ? 'Model' : 'Tool'),
              status: normalizeStepStatus(step.status),
              startedAt: step.started_at,
              completedAt: step.completed_at,
            }]
          })
        } else if (event.event === 'phase' && event.phase) {
          stream.phase = event.phase
        } else if (event.event === 'model.start') {
          const stepId = event.step_id || `model-${stream.steps.length + 1}`
          upsertExecutionStep(stream, {
            id: stepId,
            kind: 'model',
            name: 'Model',
            status: 'running',
            startedAt: event.created_at,
          })
          stream.phase = 'thinking'
        } else if (event.event === 'model.end') {
          const stepId = event.step_id
            || [...stream.steps].reverse().find((item) => item.kind === 'model' && item.status === 'running')?.id
          if (stepId) {
            upsertExecutionStep(stream, {
              id: stepId,
              kind: 'model',
              name: 'Model',
              status: normalizeStepStatus(event.step_state),
              completedAt: event.created_at,
            })
          }
          stream.phase = stream.text ? 'writing' : 'thinking'
        } else if (event.event === 'tool.start') {
          stream.phase = 'tool'
          stream.toolName = event.tool_name || '项目工具'
          stream.toolState = 'running'
          upsertExecutionStep(stream, {
            id: event.step_id || `tool-${stream.steps.length + 1}`,
            kind: 'tool',
            name: event.tool_name || 'Tool',
            status: 'running',
            startedAt: event.created_at,
          })
        } else if (event.event === 'tool.end') {
          const stepId = event.step_id
            || [...stream.steps].reverse().find((item) => item.kind === 'tool' && item.status === 'running')?.id
          if (stepId) {
            upsertExecutionStep(stream, {
              id: stepId,
              kind: 'tool',
              name: event.tool_name || stream.toolName || 'Tool',
              status: normalizeStepStatus(event.step_state || event.tool_state),
              completedAt: event.created_at,
            })
          }
          stream.toolState = event.step_state || event.tool_state || 'succeeded'
          stream.phase = stream.steps.some((item) => item.kind === 'tool' && item.status === 'running')
            ? 'tool'
            : stream.text ? 'writing' : 'thinking'
        }
        stream.updatedAt = event.created_at || new Date().toISOString()
        return
      }
      if (event.type !== 'task.updated' || !event.task_id || !event.status) return
      const task = this.tasks.find((item) => item.id === event.task_id)
      if (!task) {
        this.scheduleRefresh()
        return
      }
      task.status = event.status
      if (typeof event.progress === 'number') task.progress = Math.max(0, Math.min(100, event.progress))
      if (event.message) task.latest_message = event.message
      if (event.created_at) task.latest_event_at = event.created_at
      if (['succeeded', 'failed', 'cancelled'].includes(event.status)) this.scheduleRefresh()
    },
    async connectStream(signal: AbortSignal): Promise<void> {
      while (!signal.aborted) {
        try {
          await apiEventStream<ActivityEvent>(
            '/notifications/stream',
            ({ data }) => this.applyActivityEvent(data),
            signal,
          )
        } catch (error) {
          if (signal.aborted || (error instanceof DOMException && error.name === 'AbortError')) return
          this.streamState = 'degraded'
        }
        if (!signal.aborted) await reconnectDelay(signal)
      }
    },
    async markRead(id: string): Promise<void> {
      const item = this.notifications.find((notification) => notification.id === id)
      if (!item || item.is_read) return
      await api(`/notifications/${id}/read`, { method: 'PATCH' })
      item.is_read = true
      this.unreadCount = Math.max(0, this.unreadCount - 1)
    },
    async markAllRead(): Promise<void> {
      await api('/notifications/read-all', { method: 'POST' })
      this.notifications.forEach((item) => (item.is_read = true))
      this.unreadCount = 0
    },
    async deleteNotification(id: string): Promise<void> {
      await api(`/notifications/${id}`, { method: 'DELETE' })
      const item = this.notifications.find((notification) => notification.id === id)
      if (item && !item.is_read) this.unreadCount = Math.max(0, this.unreadCount - 1)
      this.notifications = this.notifications.filter((notification) => notification.id !== id)
    },
    async clearNotifications(): Promise<void> {
      await api('/notifications', { method: 'DELETE' })
      this.notifications = []
      this.unreadCount = 0
    },
    async retryTask(id: string): Promise<void> {
      await api(`/tasks/${id}/retry`, { method: 'POST' })
      await this.refresh()
    },
    async cancelTask(id: string): Promise<void> {
      await api(`/tasks/${id}/cancel`, { method: 'POST' })
      await this.refresh()
    },
    async deleteTask(id: string): Promise<void> {
      await api(`/tasks/${id}`, { method: 'DELETE' })
      this.tasks = this.tasks.filter((task) => task.id !== id)
      delete this.agentStreams[id]
      const removed = this.notifications.filter((notification) => notification.task_id === id)
      this.unreadCount = Math.max(
        0,
        this.unreadCount - removed.filter((notification) => !notification.is_read).length,
      )
      this.notifications = this.notifications.filter((notification) => notification.task_id !== id)
    },
    async clearTasks(group: 'terminal' | 'failed' | 'completed'): Promise<void> {
      await api(`/tasks?group=${group}`, { method: 'DELETE' })
      const removableStatuses = group === 'completed'
        ? new Set<TaskStatus>(['succeeded'])
        : group === 'failed'
          ? new Set<TaskStatus>(['failed', 'cancelled'])
          : new Set<TaskStatus>(['succeeded', 'failed', 'cancelled'])
      const removedIds = new Set(
        this.tasks.filter((task) => removableStatuses.has(task.status)).map((task) => task.id),
      )
      this.tasks = this.tasks.filter((task) => !removedIds.has(task.id))
      removedIds.forEach((id) => delete this.agentStreams[id])
      const removedNotifications = this.notifications.filter(
        (notification) => notification.task_id && removedIds.has(notification.task_id),
      )
      this.unreadCount = Math.max(
        0,
        this.unreadCount - removedNotifications.filter((notification) => !notification.is_read).length,
      )
      this.notifications = this.notifications.filter(
        (notification) => !notification.task_id || !removedIds.has(notification.task_id),
      )
    },
    async taskEvents(id: string): Promise<TaskEvent[]> {
      return api<TaskEvent[]>(`/tasks/${id}/events`)
    },
  },
})
