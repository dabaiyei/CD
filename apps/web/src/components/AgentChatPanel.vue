<script setup lang="ts">
import { useVirtualizer } from '@tanstack/vue-virtual'
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import type { ComponentPublicInstance } from 'vue'
import {
  ArrowDown,
  Bot,
  BrainCircuit,
  Check,
  ChevronDown,
  CircleCheckBig,
  Clapperboard,
  Copy,
  FilePenLine,
  FilePlus2,
  Files,
  FolderKanban,
  History,
  Image as ImageIcon,
  ImagePlus,
  LoaderCircle,
  Maximize2,
  MessageSquareText,
  Minimize2,
  PanelTopClose,
  PanelTopOpen,
  PencilLine,
  Plus,
  Search,
  Send,
  SlidersHorizontal,
  Sparkles,
  Square,
  Trash2,
  TriangleAlert,
  Video,
  WandSparkles,
  Download,
  X,
} from 'lucide-vue-next'

import { api } from '@/lib/api'
import { renderMarkdown } from '@/lib/markdown'
import { useStreamTypewriter } from '@/lib/useStreamTypewriter'
import AgentExecutionPanel from '@/components/AgentExecutionPanel.vue'
import BaseDialog from '@/components/BaseDialog.vue'
import DirectorAgentWorkflowCard from '@/components/DirectorAgentWorkflowCard.vue'
import UiSelect from '@/components/UiSelect.vue'
import { useActivityStore } from '@/stores/activity'
import { useToastStore } from '@/stores/toast'
import type {
  AITask,
  AgentChatAttachment,
  AgentChatMessage,
  AgentChatOptions,
  AgentChatRunQueued,
  AgentChatSession,
  AgentChatSessionDetail,
  AgentExecutionStep,
  AgentGeneratedMedia,
  AgentProjectFileChangeOutcome,
  DirectorWorkflowDetail,
  PricingRule,
  Project,
  UserSkill,
} from '@/types'

type PersonalAgentMode = 'chat' | 'image' | 'video' | 'skill'

defineOptions({ inheritAttrs: false })

const props = withDefaults(
  defineProps<{
    projects?: Project[]
    personal?: boolean
    initialPrompt?: string
    pricing?: PricingRule[]
    scene?: 'workspace' | 'director'
    chapterId?: string
    chapterTitle?: string
  }>(),
  { projects: () => [], personal: false, scene: 'workspace' },
)
const emit = defineEmits<{
  projectFilesChanged: [projectId: string]
  chapterChanged: [projectId: string, chapterId: string]
}>()
const toast = useToastStore()
const activity = useActivityStore()

const selectedProjectId = ref('')
const selectedSessionId = ref('')
const options = ref<AgentChatOptions>({ agents: [], skills: [] })
const sessions = ref<AgentChatSession[]>([])
const messages = ref<AgentChatMessage[]>([])
const draft = ref('')
const loading = ref(false)
const submitting = ref(false)
const runTaskId = ref('')
const textarea = ref<HTMLTextAreaElement | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)
const contextMenu = ref<HTMLDetailsElement | null>(null)
const thread = ref<HTMLDivElement | null>(null)
const stickToLatest = ref(true)
const expandedFileRuns = ref<string[]>([])
const expandedExecutionRuns = ref<string[]>([])
const liveExecutionExpanded = ref(true)
const focusMode = ref(false)
const collapsed = ref(false)
const copiedMessageId = ref('')
const cancelling = ref(false)
const uploadingAttachments = ref(false)
const pendingAttachments = ref<AgentChatAttachment[]>([])
const userSkills = ref<UserSkill[]>([])
const selectedSkillIds = ref<string[]>([])
const personalAgentMode = ref<PersonalAgentMode>('chat')
const imageAspectRatio = ref('1:1')
const imageResolution = ref('1K')
const videoAspectRatio = ref('16:9')
const videoResolution = ref('720p')
const videoDuration = ref(5)
const skillCommandIndex = ref(0)
const deleteSessionTarget = ref<AgentChatSession | null>(null)
const deletingSession = ref(false)
const directorWorkflow = ref<DirectorWorkflowDetail | null>(null)
const directorDecisionAction = ref('')
const supportedAttachmentTypes = new Set(['image/jpeg', 'image/png', 'image/webp'])
const attachmentExtensions: Record<string, string> = {
  'image/jpeg': 'jpg',
  'image/png': 'png',
  'image/webp': 'webp',
}
const maxAttachmentBytes = 8 * 1024 * 1024
const maxAttachmentCount = 4
let loadVersion = 0
let runPollTimer: ReturnType<typeof setTimeout> | undefined
let directorWorkflowPollTimer: ReturnType<typeof setTimeout> | undefined
let directorWorkflowSignature = ''
let directorWorkflowDiscoveryUntil = 0
let streamScrollFrame: number | undefined
let copiedResetTimer: ReturnType<typeof setTimeout> | undefined
const settlingTaskIds = new Set<string>()

const selectedProject = computed(() => props.projects.find((item) => item.id === selectedProjectId.value))
const personalMode = computed(() => props.personal && props.scene === 'workspace')
const personalModes: Array<{
  value: PersonalAgentMode
  label: string
  description: string
  icon: typeof MessageSquareText
}> = [
  { value: 'chat', label: '对话', description: '交流与按需调用 Skill', icon: MessageSquareText },
  { value: 'image', label: '生成图片', description: '对话打磨并直接出图', icon: ImageIcon },
  { value: 'video', label: '生成视频', description: '参考图或文本生成视频', icon: Video },
  { value: 'skill', label: '创作 Skill', description: '分析并保存新能力', icon: BrainCircuit },
]
const currentPersonalMode = computed(() => (
  personalModes.find((item) => item.value === personalAgentMode.value) ?? personalModes[0]!
))
const assistantTitle = computed(() => props.scene === 'director'
  ? 'AI 剧本创作助手'
  : personalMode.value ? '个人创作 Agent' : '制片通用助手')
const emptyGuideTitle = computed(() => props.scene === 'director'
  ? '从本章上下文开始创作'
  : personalMode.value ? currentPersonalMode.value.description : '从项目上下文开始创作')
const emptyGuideDescription = computed(() => props.scene === 'director'
  ? '直接告诉 AI 你的目标。它会读取章节与项目手册，并自动安排改编、审核、资产提取和分镜任务。'
  : personalMode.value
    ? {
        chat: '直接交流，或输入 / 为本轮显式调用一个或多个个人 Skill。',
        image: '描述想要的画面，也可以上传或粘贴图片作为视觉参考。',
        video: '描述镜头运动和内容；上传图片时会作为视频参考图使用。',
        skill: '粘贴规则或描述想学会的能力，确认后由 Agent 保存到你的 Skill 池。',
      }[personalAgentMode.value]
    : 'Agent 可读取当前项目资料，并将获准的结果同步回项目文件。')
const trackedTask = computed(() => activity.tasks.find((item) => item.id === runTaskId.value) ?? null)
const currentRunMode = computed<PersonalAgentMode>(() => {
  const value = String(trackedTask.value?.request_payload.mode || personalAgentMode.value)
  return ['chat', 'image', 'video', 'skill'].includes(value) ? value as PersonalAgentMode : 'chat'
})
const liveStream = computed(() => activity.agentStreams[runTaskId.value] ?? null)
const streamedText = computed(() => liveStream.value?.text ?? '')
const liveExecutionSteps = computed(() => liveStream.value?.steps ?? [])
const {
  displayedText: displayedStreamText,
  finish: finishStreamTyping,
  reset: resetStreamTyping,
} = useStreamTypewriter(streamedText, { onFrame: scheduleStreamScroll })
const streamedMarkdown = computed(() => renderMarkdown(displayedStreamText.value))
const sending = computed(
  () => submitting.value
    || ['queued', 'running'].includes(trackedTask.value?.status ?? '')
    || Boolean(liveStream.value),
)
const hasConversation = computed(() => messages.value.length > 0 || Boolean(selectedSessionId.value))
const canSend = computed(
  () => Boolean(
    (draft.value.trim() || pendingAttachments.value.length)
    && (personalMode.value || selectedProjectId.value)
    && options.value.agents.length
    && (props.scene !== 'director' || Boolean(props.chapterId)),
  ) && !sending.value && !uploadingAttachments.value,
)
const messageVirtualizer = useVirtualizer<HTMLDivElement, HTMLDivElement>(computed(() => ({
  count: messages.value.length,
  getScrollElement: () => thread.value,
  estimateSize: () => 132,
  getItemKey: (index: number) => messages.value[index]?.id ?? index,
  overscan: 6,
})))
const virtualMessages = computed(() => messageVirtualizer.value.getVirtualItems().flatMap((virtualRow) => {
  const message = messages.value[virtualRow.index]
  return message ? [{ virtualRow, message }] : []
}))
const virtualMessageHeight = computed(() => messageVirtualizer.value.getTotalSize())
const runStatusLabel = computed(() => {
  if (submitting.value && !trackedTask.value) return '正在提交创作任务'
  if (trackedTask.value?.status === 'queued') return '已进入创作队列'
  if (trackedTask.value?.latest_message) return trackedTask.value.latest_message
  if (liveStream.value?.phase === 'tool') return `正在使用 ${toolLabel(liveStream.value.toolName)}`
  if (liveStream.value?.phase === 'writing') return '正在生成回复'
  if (liveStream.value?.phase === 'finalizing') return '正在整理结果'
  return 'Agent 正在理解上下文并创作'
})
const enabledUserSkills = computed(() => userSkills.value.filter((skill) => skill.enabled))
const selectedUserSkills = computed(() => selectedSkillIds.value.flatMap((id) => {
  const skill = enabledUserSkills.value.find((item) => item.id === id)
  return skill ? [skill] : []
}))
const slashCommandMatch = computed(() => draft.value.match(/(?:^|\s)\/([^\s/]*)$/u))
const slashSkillQuery = computed(() => slashCommandMatch.value?.[1]?.trim().toLocaleLowerCase() ?? '')
const filteredSlashSkills = computed(() => enabledUserSkills.value.filter((skill) => {
  const query = slashSkillQuery.value
  return !query || `${skill.name} ${skill.description}`.toLocaleLowerCase().includes(query)
}))
const skillCommandOpen = computed(() => personalMode.value && Boolean(slashCommandMatch.value))
const personalPlaceholder = computed(() => ({
  chat: '聊聊你的想法，输入 / 调用 Skill',
  image: '描述想生成的画面，或上传参考图片',
  video: '描述视频内容、动作与镜头，或上传参考图',
  skill: '描述想让 AI 学会的能力，或粘贴一份 Skill 规则',
}[personalAgentMode.value]))
const personalStarters = computed(() => ({
  chat: [
    { label: '拆解一个创意', prompt: '帮我把一个模糊的故事想法拆成清晰的创作方向。', icon: MessageSquareText },
    { label: '调用我的能力', prompt: '/', icon: WandSparkles },
  ],
  image: [
    { label: '电影感人物', prompt: '生成一张具有电影光影和真实质感的人物定妆照。', icon: ImageIcon },
    { label: '动画场景', prompt: '生成一张叙事感强、空间层次清晰的动画场景概念图。', icon: Sparkles },
  ],
  video: [
    { label: '让画面动起来', prompt: '根据我上传的参考图生成自然、稳定、具有细微环境动态的视频。', icon: Video },
    { label: '设计一个镜头', prompt: '生成一个动作清晰、运镜克制、时空连续的电影镜头。', icon: Clapperboard },
  ],
  skill: [
    { label: '整理 Skill 文本', prompt: '我会粘贴一份 Skill 规则，请先分析整理，等我确认后保存。', icon: BrainCircuit },
    { label: '设计新能力', prompt: '帮我设计一个可复用的创作 Skill，先和我确认适用阶段与规则。', icon: WandSparkles },
  ],
}[personalAgentMode.value]))
const sendButtonLabel = computed(() => ({
  chat: '发送',
  image: '生成',
  video: '生成',
  skill: '发送',
}[personalAgentMode.value]))
const projectSelectOptions = computed(() => props.projects.map((project) => ({
  value: project.id,
  label: project.name,
  description: project.description || '短剧项目',
  icon: FolderKanban,
})))
const sessionSelectOptions = computed(() => sessions.value.map((session) => ({
  value: session.id,
  label: session.title,
  description: formatSessionTime(session.last_message_at),
  icon: History,
})))

watch(
  () => props.projects.map((item) => item.id).join(','),
  () => {
    if (personalMode.value) return
    const firstProject = props.projects[0]
    if (!firstProject) return
    if (!props.projects.some((item) => item.id === selectedProjectId.value)) {
      selectedProjectId.value = firstProject.id
      void loadProjectContext()
    }
  },
)

watch(runTaskId, () => {
  resetStreamTyping(streamedText.value)
  liveExecutionExpanded.value = true
  if (runTaskId.value && props.scene === 'director') requestDirectorWorkflowSync()
})

watch(filteredSlashSkills, (skills) => {
  if (!skills.length) skillCommandIndex.value = 0
  else skillCommandIndex.value = Math.min(skillCommandIndex.value, skills.length - 1)
})

watch(
  [selectedProjectId, () => props.chapterId, () => props.scene],
  () => {
    if (directorWorkflowPollTimer) clearTimeout(directorWorkflowPollTimer)
    directorWorkflow.value = null
    directorWorkflowSignature = ''
    directorWorkflowDiscoveryUntil = 0
    if (props.scene === 'director' && selectedProjectId.value && props.chapterId) {
      void loadDirectorWorkflow()
    }
  },
)

onMounted(() => {
  window.addEventListener('keydown', handleWindowKeydown)
  if (props.initialPrompt && props.scene !== 'director') draft.value = props.initialPrompt
  if (personalMode.value) void loadProjectContext()
  else {
    const firstProject = props.projects[0]
    if (!selectedProjectId.value && firstProject) {
      selectedProjectId.value = firstProject.id
      void loadProjectContext()
    }
  }
})

onUnmounted(() => {
  window.removeEventListener('keydown', handleWindowKeydown)
  if (runPollTimer) clearTimeout(runPollTimer)
  if (directorWorkflowPollTimer) clearTimeout(directorWorkflowPollTimer)
  if (streamScrollFrame !== undefined) cancelAnimationFrame(streamScrollFrame)
  if (copiedResetTimer) clearTimeout(copiedResetTimer)
  document.body.classList.remove('agent-focus-open')
})

watch(focusMode, (active) => {
  document.body.classList.toggle('agent-focus-open', active)
  if (active) collapsed.value = false
  void scrollToLatest(false)
})

watch(
  () => trackedTask.value?.status,
  (taskStatus) => {
    if (taskStatus && ['succeeded', 'failed', 'cancelled'].includes(taskStatus) && trackedTask.value) {
      void settleRunTask(trackedTask.value)
    }
  },
)

function agentApiBase(projectId: string | null | undefined = selectedProjectId.value): string {
  return personalMode.value ? '/agent' : `/projects/${projectId || selectedProjectId.value}/agent`
}

function selectProject(value: string): void {
  if (value === selectedProjectId.value) return
  void discardPendingAttachments()
  selectedProjectId.value = value
  contextMenu.value?.removeAttribute('open')
  void loadProjectContext()
}

function selectSession(value: string): void {
  if (value === selectedSessionId.value) return
  selectedSessionId.value = value
  void openSession(value)
}

async function loadProjectContext(): Promise<void> {
  if (!personalMode.value && !selectedProjectId.value) return
  const version = ++loadVersion
  loading.value = true
  messages.value = []
  options.value = { agents: [], skills: [] }
  if (personalMode.value) userSkills.value = []
  selectedSessionId.value = ''
  trackRunTask(null)
  try {
    const base = agentApiBase()
    const sceneQuery = personalMode.value ? '' : `?scene=${props.scene}`
    const [agentOptions, sessionRows, personalSkills] = await Promise.all([
      api<AgentChatOptions>(`${base}/options${sceneQuery}`),
      api<AgentChatSession[]>(`${base}/sessions${sceneQuery}`),
      personalMode.value ? api<UserSkill[]>('/user-skills') : Promise.resolve([]),
    ])
    if (version !== loadVersion) return
    options.value = agentOptions
    sessions.value = sessionRows
    userSkills.value = personalSkills
    if (!personalMode.value && sessionRows[0]) await openSession(sessionRows[0].id, version)
  } catch (error) {
    if (version !== loadVersion) return
    toast.show('Agent 加载失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    if (version === loadVersion) {
      loading.value = false
      await nextTick()
      await scrollToLatest()
    }
  }
}

function requestDirectorWorkflowSync(durationMs = 30_000): void {
  if (props.scene !== 'director' || !selectedProjectId.value || !props.chapterId) return
  directorWorkflowDiscoveryUntil = Math.max(directorWorkflowDiscoveryUntil, Date.now() + durationMs)
  scheduleDirectorWorkflowPoll(0)
}

function scheduleDirectorWorkflowPoll(delay = 1600): void {
  if (directorWorkflowPollTimer) clearTimeout(directorWorkflowPollTimer)
  directorWorkflowPollTimer = undefined
  if (props.scene !== 'director' || !selectedProjectId.value || !props.chapterId) return

  const detail = directorWorkflow.value
  const agentRunActive = Boolean(runTaskId.value)
  const waitingForUser = detail?.workflow.status === 'waiting_user' || Boolean(detail?.pending_decision)
  const discovering = Date.now() < directorWorkflowDiscoveryUntil
  if (waitingForUser && !agentRunActive) {
    directorWorkflowDiscoveryUntil = 0
    return
  }
  if (detail?.workflow.status !== 'running' && !agentRunActive && !discovering) return
  directorWorkflowPollTimer = setTimeout(() => void loadDirectorWorkflow(), delay)
}

function workflowSignature(detail: DirectorWorkflowDetail | null): string {
  if (!detail) return ''
  return JSON.stringify({
    stage: detail.workflow.stage,
    status: detail.workflow.status,
    task: detail.workflow.current_task_id,
    message: detail.workflow.last_message,
    children: detail.child_runs.map((child) => [child.id, child.status, child.attempt, child.summary]),
    decision: detail.pending_decision?.id ?? null,
  })
}

async function loadDirectorWorkflow(): Promise<void> {
  if (props.scene !== 'director' || !selectedProjectId.value || !props.chapterId) return
  if (directorWorkflowPollTimer) clearTimeout(directorWorkflowPollTimer)
  directorWorkflowPollTimer = undefined
  try {
    const detail = await api<DirectorWorkflowDetail | null>(
      `/projects/${selectedProjectId.value}/chapters/${props.chapterId}/director-workflow`,
    )
    const nextSignature = workflowSignature(detail)
    const changed = Boolean(directorWorkflowSignature && directorWorkflowSignature !== nextSignature)
    directorWorkflow.value = detail
    directorWorkflowSignature = nextSignature
    if (changed) emit('chapterChanged', selectedProjectId.value, props.chapterId)
    if (stickToLatest.value) await scrollToLatest(false)
  } catch {
    // The conversation remains usable while workflow status retries in the background.
  } finally {
    scheduleDirectorWorkflowPoll()
  }
}

async function submitDirectorDecision(option: string, feedback: string): Promise<void> {
  const detail = directorWorkflow.value
  const decision = detail?.pending_decision
  if (!detail || !decision || directorDecisionAction.value) return
  if (option === 'provide_feedback' && !feedback) {
    toast.show('请先补充修改意见', { tone: 'info' })
    return
  }
  directorDecisionAction.value = option
  try {
    directorWorkflow.value = await api<DirectorWorkflowDetail>(
      `/projects/${selectedProjectId.value}/director-workflows/${detail.workflow.id}/decisions/${decision.id}`,
      {
        method: 'POST',
        body: JSON.stringify({ option, feedback }),
      },
    )
    directorWorkflowSignature = workflowSignature(directorWorkflow.value)
    toast.show('导演 Agent 已接收你的决定', { message: '修复任务会完成后自动重新审核', tone: 'success' })
    emit('chapterChanged', selectedProjectId.value, props.chapterId || '')
  } catch (error) {
    toast.show('审核选择提交失败', {
      message: error instanceof Error ? error.message : undefined,
      tone: 'error',
    })
  } finally {
    directorDecisionAction.value = ''
    scheduleDirectorWorkflowPoll()
  }
}

async function openSession(sessionId: string, version = loadVersion): Promise<void> {
  if (!sessionId || (!personalMode.value && !selectedProjectId.value)) return
  try {
    const detail = await fetchSessionDetail(sessionId)
    await applySessionDetail(detail, version)
  } catch (error) {
    toast.show('会话加载失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  }
}

function fetchSessionDetail(sessionId: string): Promise<AgentChatSessionDetail> {
  return api<AgentChatSessionDetail>(`${agentApiBase()}/sessions/${sessionId}`)
}

async function applySessionDetail(
  detail: AgentChatSessionDetail,
  version = loadVersion,
  trackActiveTask = true,
): Promise<boolean> {
  if (version !== loadVersion) return false
  selectedSessionId.value = detail.session.id
  messages.value = detail.messages
  if (personalMode.value) {
    const storedMode = String(
      detail.active_task?.request_payload.mode
      || detail.session.runtime_manifest?.mode
      || detail.session.runtime_manifest?.last_mode
      || '',
    )
    if (['chat', 'image', 'video', 'skill'].includes(storedMode)) {
      personalAgentMode.value = storedMode as PersonalAgentMode
    }
  }
  if (trackActiveTask) trackRunTask(detail.active_task)
  await scrollToLatest()
  return true
}

function startNewConversation(): void {
  if (sending.value) return
  void discardPendingAttachments()
  trackRunTask(null)
  selectedSessionId.value = ''
  messages.value = []
  draft.value = ''
  selectedSkillIds.value = []
  resizeTextarea()
  void nextTick(() => textarea.value?.focus())
}

function selectPersonalMode(mode: PersonalAgentMode): void {
  if (!personalMode.value || sending.value || personalAgentMode.value === mode) return
  personalAgentMode.value = mode
  skillCommandIndex.value = 0
  void nextTick(() => textarea.value?.focus())
}

function removeSelectedSkill(skillId: string): void {
  selectedSkillIds.value = selectedSkillIds.value.filter((id) => id !== skillId)
}

function selectSlashSkill(skill: UserSkill): void {
  selectedSkillIds.value = selectedSkillIds.value.includes(skill.id)
    ? selectedSkillIds.value.filter((id) => id !== skill.id)
    : [...selectedSkillIds.value, skill.id]
  const match = slashCommandMatch.value
  if (match?.index !== undefined) {
    const leadingWhitespace = /^\s/u.test(match[0]) ? match[0][0] : ''
    draft.value = `${draft.value.slice(0, match.index)}${leadingWhitespace}${draft.value.slice(match.index + match[0].length)}`
  }
  skillCommandIndex.value = 0
  void nextTick(() => {
    resizeTextarea()
    textarea.value?.focus()
  })
}

function handleComposerKeydown(event: KeyboardEvent): void {
  if (skillCommandOpen.value) {
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      if (filteredSlashSkills.value.length) {
        skillCommandIndex.value = (skillCommandIndex.value + 1) % filteredSlashSkills.value.length
      }
      return
    }
    if (event.key === 'ArrowUp') {
      event.preventDefault()
      if (filteredSlashSkills.value.length) {
        skillCommandIndex.value = (
          skillCommandIndex.value - 1 + filteredSlashSkills.value.length
        ) % filteredSlashSkills.value.length
      }
      return
    }
    if (event.key === 'Enter' && !event.shiftKey) {
      const skill = filteredSlashSkills.value[skillCommandIndex.value]
      if (skill) {
        event.preventDefault()
        selectSlashSkill(skill)
        return
      }
    }
    if (event.key === 'Escape') {
      event.preventDefault()
      const match = slashCommandMatch.value
      if (match?.index !== undefined) {
        draft.value = `${draft.value.slice(0, match.index)}${draft.value.slice(match.index + match[0].length)}`
      }
      return
    }
  }
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    void sendMessage()
  }
}

function requestDeleteSession(): void {
  if (sending.value || !selectedSessionId.value) return
  deleteSessionTarget.value = sessions.value.find((item) => item.id === selectedSessionId.value) ?? null
}

async function deleteCurrentSession(): Promise<void> {
  const target = deleteSessionTarget.value
  if (!target || deletingSession.value) return
  deletingSession.value = true
  try {
    await api(`${agentApiBase(target.project_id)}/sessions/${target.id}`, { method: 'DELETE' })
    sessions.value = sessions.value.filter((item) => item.id !== target.id)
    deleteSessionTarget.value = null
    selectedSessionId.value = ''
    messages.value = []
    trackRunTask(null)
    await discardPendingAttachments()
    const nextSession = sessions.value[0]
    if (nextSession) await openSession(nextSession.id)
    else startNewConversation()
    toast.show('对话已删除', { message: '历史消息与对话图片已清理', tone: 'success' })
  } catch (error) {
    toast.show('无法删除对话', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    deletingSession.value = false
  }
}

function handleWindowKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape' && focusMode.value) focusMode.value = false
}

function toggleFocusMode(): void {
  focusMode.value = !focusMode.value
}

function toggleCollapsed(): void {
  if (!collapsed.value && focusMode.value) focusMode.value = false
  collapsed.value = !collapsed.value
}

async function copyMessage(message: AgentChatMessage): Promise<void> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(message.content)
    } else {
      const temporary = document.createElement('textarea')
      temporary.value = message.content
      temporary.style.position = 'fixed'
      temporary.style.opacity = '0'
      document.body.appendChild(temporary)
      temporary.select()
      document.execCommand('copy')
      temporary.remove()
    }
    copiedMessageId.value = message.id
    if (copiedResetTimer) clearTimeout(copiedResetTimer)
    copiedResetTimer = setTimeout(() => (copiedMessageId.value = ''), 1600)
  } catch {
    toast.show('复制失败', { message: '浏览器未授予剪贴板权限', tone: 'error' })
  }
}

function editMessage(message: AgentChatMessage): void {
  draft.value = message.content
  collapsed.value = false
  void nextTick(() => {
    resizeTextarea()
    textarea.value?.focus()
  })
}

async function stopCurrentRun(): Promise<void> {
  if (!trackedTask.value || !['queued', 'running'].includes(trackedTask.value.status) || cancelling.value) return
  const taskId = trackedTask.value.id
  cancelling.value = true
  try {
    await activity.cancelTask(taskId)
    toast.show('已停止生成', { message: '本轮 Agent 创作已取消并停止继续输出', tone: 'info' })
    const task = await api<AITask>(`/tasks/${taskId}`)
    activity.upsertTask(task)
    await settleRunTask(task)
  } catch (error) {
    toast.show('无法停止生成', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    cancelling.value = false
  }
}

function messageAttachments(message: AgentChatMessage): AgentChatAttachment[] {
  const value = message.runtime_manifest?.attachments
  if (!Array.isArray(value)) return []
  return value.filter((item): item is AgentChatAttachment => {
    if (!item || typeof item !== 'object') return false
    const row = item as Record<string, unknown>
    return typeof row.id === 'string'
      && typeof row.name === 'string'
      && typeof row.media_url === 'string'
      && typeof row.mime_type === 'string'
  })
}

function messageGeneratedMedia(message: AgentChatMessage): AgentGeneratedMedia[] {
  const value = message.runtime_manifest?.generated_media
  if (!Array.isArray(value)) return []
  return value.filter((item): item is AgentGeneratedMedia => {
    if (!item || typeof item !== 'object') return false
    const row = item as Record<string, unknown>
    return typeof row.id === 'string'
      && typeof row.media_url === 'string'
      && typeof row.mime_type === 'string'
      && typeof row.prompt === 'string'
  })
}

function messageModeLabel(message: AgentChatMessage): string {
  return {
    chat: '对话',
    image: '图片生成',
    video: '视频生成',
    skill: 'Skill 创作',
  }[String(message.runtime_manifest?.mode || '')] || ''
}

function messageSelectedSkills(message: AgentChatMessage): Array<{ id: string; name: string }> {
  const value = message.runtime_manifest?.selected_skills
  if (!Array.isArray(value)) return []
  return value.flatMap((item) => {
    if (!item || typeof item !== 'object') return []
    const row = item as Record<string, unknown>
    return typeof row.id === 'string' && typeof row.name === 'string'
      ? [{ id: row.id, name: row.name }]
      : []
  })
}

function mediaModeLabel(mode: string | null): string {
  return {
    text_to_video: '文生视频',
    first_frame: '首帧参考',
    first_last_frame: '首尾帧参考',
    full_reference: '全参考生成',
    multi_shot: '多图参考',
  }[mode || ''] || mode || ''
}

function mediaSizeLabel(sizeBytes: number): string {
  if (sizeBytes < 1024 * 1024) return `${Math.max(1, Math.round(sizeBytes / 1024))} KB`
  return `${(sizeBytes / 1024 / 1024).toFixed(1)} MB`
}

async function copyMediaPrompt(media: AgentGeneratedMedia): Promise<void> {
  try {
    await navigator.clipboard.writeText(media.prompt)
    copiedMessageId.value = `media-${media.id}`
    if (copiedResetTimer) clearTimeout(copiedResetTimer)
    copiedResetTimer = setTimeout(() => (copiedMessageId.value = ''), 1600)
  } catch {
    toast.show('提示词复制失败', { message: '浏览器未授予剪贴板权限', tone: 'error' })
  }
}

function openAttachmentPicker(): void {
  if (!sending.value && !uploadingAttachments.value && pendingAttachments.value.length < maxAttachmentCount) {
    fileInput.value?.click()
  }
}

async function uploadAttachments(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  input.value = ''
  await uploadAttachmentFiles(files)
}

async function pasteAttachments(event: ClipboardEvent): Promise<void> {
  const clipboard = event.clipboardData
  if (!clipboard) return
  const itemFiles = Array.from(clipboard.items)
    .filter((item) => item.kind === 'file' && item.type.startsWith('image/'))
    .map((item) => item.getAsFile())
    .filter((file): file is File => Boolean(file))
  const files = itemFiles.length
    ? itemFiles
    : Array.from(clipboard.files).filter((file) => file.type.startsWith('image/'))
  if (!files.length) return

  event.preventDefault()
  if (sending.value) {
    toast.show('生成期间无法添加图片', { message: '停止或等待本轮创作完成后再粘贴', tone: 'info' })
    return
  }
  if (uploadingAttachments.value) {
    toast.show('图片正在处理中', { message: '当前图片上传完成后可继续粘贴', tone: 'info' })
    return
  }
  await uploadAttachmentFiles(files, true)
}

async function uploadAttachmentFiles(files: File[], fromClipboard = false): Promise<void> {
  if (!files.length || (!personalMode.value && !selectedProjectId.value)) return
  const available = maxAttachmentCount - pendingAttachments.value.length
  if (available <= 0) {
    toast.show(`最多添加 ${maxAttachmentCount} 张图片`, { tone: 'info' })
    return
  }
  if (files.length > available) {
    toast.show(`最多添加 ${maxAttachmentCount} 张图片`, { message: `当前还可以添加 ${available} 张`, tone: 'info' })
  }
  uploadingAttachments.value = true
  let uploadedCount = 0
  try {
    for (const [index, file] of files.slice(0, available).entries()) {
      const fileLabel = file.name || '剪贴板图片'
      if (!supportedAttachmentTypes.has(file.type)) {
        toast.show('图片格式不支持', { message: `${fileLabel} 不是 JPG、PNG 或 WebP`, tone: 'error' })
        continue
      }
      if (file.size > maxAttachmentBytes) {
        toast.show('图片过大', { message: `${fileLabel} 超过 8MB`, tone: 'error' })
        continue
      }
      const body = new FormData()
      const extension = attachmentExtensions[file.type]
      const filename = file.name || `clipboard-${Date.now()}-${index + 1}.${extension}`
      body.append('file', file, filename)
      const attachment = await api<AgentChatAttachment>(
        `${agentApiBase()}/attachments`,
        { method: 'POST', body },
      )
      pendingAttachments.value.push(attachment)
      uploadedCount += 1
    }
    if (fromClipboard && uploadedCount) {
      toast.show(`已粘贴 ${uploadedCount} 张图片`, { message: '可继续输入文字或直接发送', tone: 'success' })
    }
  } catch (error) {
    toast.show('图片上传失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    uploadingAttachments.value = false
    await nextTick()
    textarea.value?.focus()
  }
}

async function removePendingAttachment(attachment: AgentChatAttachment): Promise<void> {
  try {
    await api(`${agentApiBase(attachment.project_id)}/attachments/${attachment.id}`, { method: 'DELETE' })
    pendingAttachments.value = pendingAttachments.value.filter((item) => item.id !== attachment.id)
  } catch (error) {
    toast.show('无法移除图片', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  }
}

async function discardPendingAttachments(): Promise<void> {
  const attachments = pendingAttachments.value
  pendingAttachments.value = []
  await Promise.allSettled(
    attachments.map((item) => api(
      `${agentApiBase(item.project_id)}/attachments/${item.id}`,
      { method: 'DELETE' },
    )),
  )
}

function useStarter(content: string): void {
  draft.value = props.scene === 'director' && props.initialPrompt
    ? `${props.initialPrompt}\n\n${content}`
    : content
  void nextTick(() => {
    resizeTextarea()
    textarea.value?.focus()
  })
}

function resizeTextarea(): void {
  if (!textarea.value) return
  textarea.value.style.height = 'auto'
  textarea.value.style.height = `${Math.min(textarea.value.scrollHeight, 176)}px`
}

function measureMessageRow(element: Element | ComponentPublicInstance | null): void {
  const node = element instanceof Element ? element : element?.$el
  if (node instanceof HTMLDivElement) messageVirtualizer.value.measureElement(node)
}

async function scrollToLatest(force = true): Promise<void> {
  await nextTick()
  if (!thread.value || (!force && !stickToLatest.value)) return
  messageVirtualizer.value.measure()
  await nextTick()
  if (messages.value.length) {
    messageVirtualizer.value.scrollToIndex(messages.value.length - 1, { align: 'end' })
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()))
  }
  thread.value.scrollTop = thread.value.scrollHeight
  stickToLatest.value = true
}

function scheduleStreamScroll(): void {
  if (!stickToLatest.value || streamScrollFrame !== undefined) return
  streamScrollFrame = requestAnimationFrame(() => {
    streamScrollFrame = undefined
    if (!thread.value || !stickToLatest.value) return
    thread.value.scrollTop = thread.value.scrollHeight
  })
}

function updateScrollIntent(): void {
  if (!thread.value) return
  stickToLatest.value = thread.value.scrollHeight - thread.value.scrollTop - thread.value.clientHeight < 72
}

function containConversationScroll(event: WheelEvent): void {
  if (event.ctrlKey || Math.abs(event.deltaY) <= Math.abs(event.deltaX)) return

  const studio = event.currentTarget
  if (!(studio instanceof HTMLElement) || !studio.contains(document.activeElement)) return

  const messageThread = thread.value
  event.stopPropagation()

  if (!messageThread) {
    event.preventDefault()
    return
  }

  const eventStartedInThread = event.composedPath().includes(messageThread)
  const atTop = messageThread.scrollTop <= 0
  const atBottom = messageThread.scrollTop + messageThread.clientHeight >= messageThread.scrollHeight - 1
  const reachedBoundary = (event.deltaY < 0 && atTop) || (event.deltaY > 0 && atBottom)

  if (eventStartedInThread && !reachedBoundary) return

  event.preventDefault()
  const deltaScale = event.deltaMode === WheelEvent.DOM_DELTA_LINE
    ? 16
    : event.deltaMode === WheelEvent.DOM_DELTA_PAGE
      ? messageThread.clientHeight
      : 1
  messageThread.scrollTop += event.deltaY * deltaScale
}

function toolLabel(name: string): string {
  return {
    Read: '文件读取',
    Write: '文件写入',
    Edit: '文件编辑',
    Glob: '文件检索',
    Grep: '内容搜索',
    Skill: '项目技能',
  }[name] || '项目工具'
}

function formatSessionTime(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric' }).format(new Date(value))
}

function fileChanges(message: AgentChatMessage): AgentProjectFileChangeOutcome[] {
  const value = message.runtime_manifest?.project_file_changes
  if (!Array.isArray(value)) return []
  return value.filter((item): item is AgentProjectFileChangeOutcome => {
    if (!item || typeof item !== 'object') return false
    const row = item as Record<string, unknown>
    return typeof row.operation === 'string'
      && typeof row.name === 'string'
      && ['applied', 'conflict', 'rejected'].includes(String(row.status))
  })
}

function fileChangeCount(message: AgentChatMessage, status: AgentProjectFileChangeOutcome['status']): number {
  return fileChanges(message).filter((item) => item.status === status).length
}

function fileChangeLabel(operation: AgentProjectFileChangeOutcome['operation']): string {
  return {
    create: '新建',
    update: '更新',
    delete: '删除',
    batch: '批量操作',
    publish_script_version: '发布剧本',
    publish_storyboard_version: '发布分镜',
    queue_asset_prompt_generation: '排队生成提示词',
    queue_asset_image_generation: '排队生成图片',
    queue_storyboard_workflow: '启动分镜工作流',
  }[operation]
}

function fileSyncTitle(message: AgentChatMessage): string {
  const changes = fileChanges(message)
  if (changes.some((item) => item.operation.startsWith('queue_asset_'))) return '平台任务已安排'
  if (changes.some((item) => item.operation === 'queue_storyboard_workflow')) return '导演工作流已启动'
  if (changes.some((item) => item.operation === 'publish_storyboard_version')) return '分镜已同步'
  if (changes.some((item) => item.operation === 'publish_script_version')) return '剧本已同步'
  return '项目文件已同步'
}

function fileChangeDetail(change: AgentProjectFileChangeOutcome): string {
  if (change.operation === 'queue_asset_prompt_generation') {
    const count = change.asset_count ?? change.asset_ids?.length ?? 0
    return `${count} 个资产提示词任务已排队${change.auto_queue_images_after_prompt ? '，完成后自动进入生图' : ''}`
  }
  if (change.operation === 'queue_asset_image_generation') {
    const imageCount = change.asset_count ?? change.asset_ids?.length ?? 0
    const promptCount = change.prompt_asset_count ?? change.prompt_asset_ids?.length ?? 0
    if (promptCount && imageCount) return `${promptCount} 个资产先补提示词，${imageCount} 个资产已排队生图`
    if (promptCount) return `${promptCount} 个资产先补提示词，完成后自动进入生图`
    return `${imageCount} 个资产生图任务已排队`
  }
  if (change.operation === 'queue_storyboard_workflow') {
    return `分镜工作流已接管，当前阶段：${change.stage || '排队中'}`
  }
  return fileChangeLabel(change.operation)
}

function fileRunExpanded(messageId: string): boolean {
  return expandedFileRuns.value.includes(messageId)
}

function executionRunExpanded(messageId: string): boolean {
  return expandedExecutionRuns.value.includes(messageId)
}

function toggleExecutionRun(messageId: string): void {
  expandedExecutionRuns.value = executionRunExpanded(messageId)
    ? expandedExecutionRuns.value.filter((id) => id !== messageId)
    : [...expandedExecutionRuns.value, messageId]
  void nextTick(() => messageVirtualizer.value.measure())
}

function normalizeExecutionStatus(value: unknown): AgentExecutionStep['status'] {
  const status = String(value || '').toLowerCase()
  if (['error', 'failed', 'failure', 'cancelled', 'canceled'].includes(status)) return 'failed'
  if (['running', 'started', 'pending'].includes(status)) return 'running'
  return 'succeeded'
}

function executionSteps(message: AgentChatMessage): AgentExecutionStep[] {
  const steps: AgentExecutionStep[] = []
  const byId = new Map<string, AgentExecutionStep>()
  const activeModels: string[] = []
  const activeTools: string[] = []
  let sequence = 0
  for (const event of message.runtime_events || []) {
    const type = String(event.type || '')
    const createdAt = typeof event.created_at === 'string' ? event.created_at : undefined
    if (type === 'MODEL_CALL_START') {
      const id = String(event.model_call_id || '') || `model-${++sequence}`
      const step: AgentExecutionStep = {
        id,
        kind: 'model',
        name: 'Model',
        status: 'running',
        startedAt: createdAt,
      }
      steps.push(step)
      byId.set(id, step)
      activeModels.push(id)
    } else if (type === 'MODEL_CALL_END') {
      const id = String(event.model_call_id || '') || activeModels.pop()
      const step = id ? byId.get(id) : undefined
      if (step) {
        step.status = normalizeExecutionStatus(event.state)
        step.completedAt = createdAt
      }
    } else if (type === 'TOOL_CALL_START') {
      const id = String(event.tool_call_id || '') || `tool-${++sequence}`
      const step: AgentExecutionStep = {
        id,
        kind: 'tool',
        name: String(event.tool_call_name || 'Tool'),
        status: 'running',
        startedAt: createdAt,
      }
      steps.push(step)
      byId.set(id, step)
      activeTools.push(id)
    } else if (type === 'TOOL_RESULT_END') {
      const requestedId = String(event.tool_call_id || '')
      const id = requestedId || activeTools.pop()
      const step = id ? byId.get(id) : undefined
      if (step) {
        step.status = normalizeExecutionStatus(event.state)
        step.completedAt = createdAt
        const activeIndex = activeTools.indexOf(step.id)
        if (activeIndex >= 0) activeTools.splice(activeIndex, 1)
      }
    }
  }
  if (message.finish_reason) {
    steps.forEach((step) => {
      if (step.status === 'running') step.status = 'succeeded'
    })
  }
  return steps
}

function toggleFileRun(messageId: string): void {
  expandedFileRuns.value = fileRunExpanded(messageId)
    ? expandedFileRuns.value.filter((id) => id !== messageId)
    : [...expandedFileRuns.value, messageId]
}

function trackRunTask(task: AITask | null): void {
  if (runPollTimer) clearTimeout(runPollTimer)
  runPollTimer = undefined
  if (!task) {
    runTaskId.value = ''
    return
  }
  activity.upsertTask(task)
  runTaskId.value = task.id
  activity.beginAgentStream(task.id, String(task.request_payload.agent_chat_session_id ?? ''))
  if (['queued', 'running'].includes(task.status)) scheduleRunPoll()
}

function scheduleRunPoll(delay = 1400): void {
  if (runPollTimer) clearTimeout(runPollTimer)
  runPollTimer = setTimeout(() => void pollRunTask(), delay)
}

async function pollRunTask(): Promise<void> {
  const taskId = runTaskId.value
  if (!taskId) return
  try {
    const task = await api<AITask>(`/tasks/${taskId}`)
    activity.upsertTask(task)
    if (['queued', 'running'].includes(task.status)) scheduleRunPoll()
    else await settleRunTask(task)
  } catch {
    if (runTaskId.value === taskId) scheduleRunPoll(3000)
  }
}

async function settleRunTask(task: AITask): Promise<void> {
  if (settlingTaskIds.has(task.id)) return
  settlingTaskIds.add(task.id)
  const taskSessionId = String(task.request_payload.agent_chat_session_id ?? '')
  try {
    if (taskSessionId && taskSessionId === selectedSessionId.value) {
      const detail = await fetchSessionDetail(taskSessionId)
      const assistant = detail.messages.find((item) => item.role === 'assistant' && item.run_id === task.id)
      if (task.status === 'succeeded' && assistant?.content) {
        activity.reconcileAgentStreamText(task.id, assistant.content)
      }
      await finishStreamTyping()
      activity.clearAgentStream(task.id)
      if (runTaskId.value === task.id) runTaskId.value = ''
      const applied = await applySessionDetail(detail, loadVersion, false)
      if (!applied) return
      if (task.status === 'succeeded') {
        if (assistant && fileChangeCount(assistant, 'applied')) {
          if (selectedProjectId.value) emit('projectFilesChanged', selectedProjectId.value)
        }
        const publishedScript = assistant
          ? fileChanges(assistant).find((item) => (
              item.status === 'applied'
              && item.resource_type === 'script_version'
              && item.chapter_id
            ))
          : undefined
        if (publishedScript?.chapter_id) {
          emit('chapterChanged', selectedProjectId.value, publishedScript.chapter_id)
        }
        if (props.scene === 'director') {
          requestDirectorWorkflowSync()
          await loadDirectorWorkflow()
        }
      } else {
        toast.show(task.status === 'cancelled' ? 'Agent 创作已取消' : 'Agent 创作失败', {
          message: task.error_message ?? task.latest_message ?? undefined,
          tone: task.status === 'cancelled' ? 'info' : 'error',
        })
      }
    } else if (runTaskId.value === task.id) {
      trackRunTask(null)
    }
  } catch (error) {
    toast.show('Agent 结果同步失败', {
      message: error instanceof Error ? error.message : undefined,
      tone: 'error',
    })
  } finally {
    activity.clearAgentStream(task.id)
    if (runTaskId.value === task.id) trackRunTask(null)
  }
}

async function sendMessage(): Promise<void> {
  const content = draft.value.trim()
  const attachments = [...pendingAttachments.value]
  const submittedSkillIds = [...selectedSkillIds.value]
  const submittedMode = personalAgentMode.value
  const mediaOptions = submittedMode === 'image'
    ? { resolution: imageResolution.value, aspect_ratio: imageAspectRatio.value }
    : submittedMode === 'video'
      ? {
          resolution: videoResolution.value,
          aspect_ratio: videoAspectRatio.value,
          duration_seconds: videoDuration.value,
        }
      : {}
  if (!canSend.value || (!content && !attachments.length)) return
  const submittedAt = Date.now()
  const pendingMessageId = `pending-${submittedAt}`
  submitting.value = true
  draft.value = ''
  pendingAttachments.value = []
  selectedSkillIds.value = []
  resizeTextarea()
  let sessionId = selectedSessionId.value
  try {
    if (!sessionId) {
      const created = await api<AgentChatSession>(`${agentApiBase()}/sessions`, {
        method: 'POST',
        body: JSON.stringify({ scene: props.scene }),
      })
      sessionId = created.id
      selectedSessionId.value = created.id
      sessions.value.unshift(created)
    }

    const selectedSkillManifest = submittedSkillIds.flatMap((id) => {
      const skill = userSkills.value.find((item) => item.id === id)
      return skill ? [{ id: skill.id, name: skill.name, version: skill.version }] : []
    })
    messages.value.push({
      id: pendingMessageId,
      session_id: sessionId,
      role: 'user',
      content,
      run_id: null,
      finish_reason: null,
      runtime_events: [],
      runtime_manifest: {
        ...(attachments.length ? { attachments } : {}),
        ...(personalMode.value
          ? {
              mode: submittedMode,
              selected_skills: selectedSkillManifest,
              media_options: mediaOptions,
            }
          : {}),
      },
      created_at: new Date().toISOString(),
    })
    await scrollToLatest()

    const result = await api<AgentChatRunQueued>(
      `${agentApiBase()}/sessions/${sessionId}/messages`,
      {
        method: 'POST',
        body: JSON.stringify({
          content,
          attachment_ids: attachments.map((item) => item.id),
          chapter_id: props.scene === 'director' ? props.chapterId : undefined,
          mode: personalMode.value ? submittedMode : undefined,
          skill_ids: personalMode.value ? submittedSkillIds : undefined,
          media_options: personalMode.value ? mediaOptions : undefined,
        }),
      },
    )
    const pendingIndex = messages.value.findIndex((item) => item.id === pendingMessageId)
    if (pendingIndex >= 0) messages.value[pendingIndex] = result.user_message
    if (result.task.task_type === 'agent_chat_run') {
      trackRunTask(result.task)
    } else {
      activity.upsertTask(result.task)
      const detail = await fetchSessionDetail(sessionId)
      await applySessionDetail(detail, loadVersion, false)
      if (selectedProjectId.value) emit('projectFilesChanged', selectedProjectId.value)
      if (props.scene === 'director') {
        requestDirectorWorkflowSync()
        await loadDirectorWorkflow()
      }
    }
    stickToLatest.value = true
    const sessionIndex = sessions.value.findIndex((item) => item.id === result.session.id)
    if (sessionIndex >= 0) sessions.value.splice(sessionIndex, 1)
    sessions.value.unshift(result.session)
    await scrollToLatest()
  } catch (error) {
    toast.show('Agent 暂时无法回复', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
    if (sessionId) await openSession(sessionId)
    const accepted = messages.value.some((message) => (
      message.role === 'user'
      && message.content === content
      && new Date(message.created_at).getTime() >= submittedAt - 1000
    ))
    if (!accepted) {
      draft.value = content
      pendingAttachments.value = attachments
      selectedSkillIds.value = submittedSkillIds
      resizeTextarea()
    }
  } finally {
    submitting.value = false
    await nextTick()
    textarea.value?.focus()
  }
}
</script>

<template>
  <Teleport to="body" :disabled="!focusMode">
    <section
      v-bind="$attrs"
      class="agent-studio"
      :class="{ 'agent-studio--focus': focusMode, 'agent-studio--collapsed': collapsed, 'agent-studio--personal': personalMode }"
      aria-labelledby="agent-studio-title"
      @wheel="containConversationScroll"
    >
    <div v-if="loading" class="agent-composer-shell agent-composer-shell--loading" aria-label="正在加载 Agent">
      <LoaderCircle class="spin" :size="24" />
    </div>

    <div v-else-if="!options.agents.length" class="agent-composer-shell agent-composer-empty">
      <Bot :size="22" />
      <strong>暂无可用 Agent</strong>
      <span>请联系管理员完成 Agent 与文本模型配置</span>
    </div>

    <div v-else class="agent-composer-shell" :class="{ 'agent-composer-shell--active': hasConversation }">
      <header class="agent-workbench-header">
        <div class="agent-workbench-header__identity">
          <div v-if="scene === 'director' || personalMode" class="agent-context-switcher agent-context-switcher--static">
            <span class="agent-avatar"><Sparkles :size="16" /></span>
            <span class="agent-context-switcher__copy">
              <strong id="agent-studio-title">{{ assistantTitle }}</strong>
              <small v-if="personalMode"><Bot :size="12" />独立会话 · 不访问项目</small>
              <small v-else><FolderKanban :size="12" />{{ selectedProject?.name || '未选择项目' }}</small>
            </span>
          </div>
          <details v-else ref="contextMenu" class="agent-context-switcher">
            <summary title="切换项目">
              <span class="agent-avatar"><Sparkles :size="16" /></span>
              <span class="agent-context-switcher__copy">
                <strong id="agent-studio-title">{{ assistantTitle }}</strong>
                <small><FolderKanban :size="12" />{{ selectedProject?.name || '未选择项目' }}</small>
              </span>
              <ChevronDown :size="14" />
            </summary>
            <div class="agent-context-switcher__popover">
              <label>
                <span>当前项目</span>
                <UiSelect :model-value="selectedProjectId" :options="projectSelectOptions" :disabled="sending" placeholder="选择项目" @update:model-value="selectProject" />
              </label>
            </div>
          </details>
          <span v-if="sending" class="agent-workbench-header__status">
            <LoaderCircle class="spin" :size="12" />{{ runStatusLabel }}
          </span>
        </div>

        <div class="agent-workbench-header__actions">
          <UiSelect
            v-if="sessions.length"
            :model-value="selectedSessionId"
            :options="sessionSelectOptions"
            :disabled="sending"
            placeholder="历史会话"
            variant="history"
            @update:model-value="selectSession"
          />
          <button class="agent-toolbar-button agent-toolbar-button--new" type="button" :disabled="sending" title="开始新对话" @click="startNewConversation">
            <Plus :size="17" /><span>新对话</span>
          </button>
          <button class="agent-toolbar-button agent-toolbar-button--danger" type="button" :disabled="!selectedSessionId || sending" title="删除当前对话" @click="requestDeleteSession">
            <Trash2 :size="17" />
          </button>
          <button class="agent-toolbar-button" type="button" :title="focusMode ? '退出专注模式' : '进入专注模式'" @click="toggleFocusMode">
            <Minimize2 v-if="focusMode" :size="17" />
            <Maximize2 v-else :size="17" />
          </button>
          <button v-if="scene !== 'director'" class="agent-toolbar-button" type="button" :aria-expanded="!collapsed" :title="collapsed ? '展开对话窗口' : '收起对话窗口'" @click="toggleCollapsed">
            <PanelTopOpen v-if="collapsed" :size="18" />
            <PanelTopClose v-else :size="18" />
          </button>
        </div>
      </header>

      <div v-if="!collapsed" class="agent-workspace-body">
        <nav v-if="personalMode" class="agent-mode-switcher" aria-label="个人 Agent 模式">
          <button
            v-for="mode in personalModes"
            :key="mode.value"
            type="button"
            :aria-pressed="personalAgentMode === mode.value"
            :disabled="sending"
            :title="mode.description"
            @click="selectPersonalMode(mode.value)"
          >
            <span class="agent-mode-switcher__icon"><component :is="mode.icon" :size="17" /></span>
            <span><strong>{{ mode.label }}</strong></span>
          </button>
        </nav>
        <div v-if="hasConversation" class="agent-conversation">
          <div ref="thread" class="agent-thread" :class="{ 'is-streaming': sending }" aria-live="off" @scroll.passive="updateScrollIntent">
            <div class="agent-thread__virtual" :style="{ height: `${virtualMessageHeight}px` }">
              <div
                v-for="{ virtualRow, message } in virtualMessages"
                :key="message.id"
                :ref="measureMessageRow"
                class="agent-message-row"
                :class="`agent-message-row--${message.role}`"
                :data-index="virtualRow.index"
                :style="{ transform: `translateY(${virtualRow.start}px)` }"
              >
                <article
                  class="agent-message"
                  :class="`agent-message--${message.role}`"
                >
              <span v-if="message.role === 'assistant'" class="agent-avatar"><Sparkles :size="15" /></span>
              <div class="agent-message__body">
                <div
                  v-if="message.role === 'user' && (messageModeLabel(message) || messageSelectedSkills(message).length)"
                  class="agent-message-context"
                >
                  <span v-if="messageModeLabel(message)">{{ messageModeLabel(message) }}</span>
                  <span v-for="skill in messageSelectedSkills(message)" :key="skill.id"><BrainCircuit :size="11" />{{ skill.name }}</span>
                </div>
                <div v-if="messageAttachments(message).length" class="agent-message-attachments">
                  <figure v-for="attachment in messageAttachments(message)" :key="attachment.id">
                    <img :src="attachment.media_url" :alt="attachment.name" />
                    <figcaption>{{ attachment.name }}</figcaption>
                  </figure>
                </div>
                <div
                  v-if="message.role === 'assistant' && message.content"
                  class="agent-message__content agent-markdown"
                  v-html="renderMarkdown(message.content)"
                ></div>
                <div v-else-if="message.content" class="agent-message__content">{{ message.content }}</div>

                <section
                  v-for="media in messageGeneratedMedia(message)"
                  :key="media.id"
                  class="agent-generated-media"
                  :data-kind="media.mime_type.startsWith('video/') ? 'video' : 'image'"
                >
                  <figure>
                    <video
                      v-if="media.mime_type.startsWith('video/')"
                      :src="media.media_url"
                      controls
                      preload="metadata"
                      playsinline
                    ></video>
                    <a v-else :href="media.media_url" target="_blank" rel="noopener" title="查看原图">
                      <img :src="media.media_url" :alt="media.name" />
                    </a>
                  </figure>
                  <div class="agent-generated-media__info">
                    <div>
                      <span class="agent-generated-media__kind">
                        <Video v-if="media.mime_type.startsWith('video/')" :size="14" />
                        <ImageIcon v-else :size="14" />
                      </span>
                      <span>
                        <strong>{{ media.name }}</strong>
                        <small>
                          {{ media.model_name }} · {{ media.resolution }} · {{ media.aspect_ratio }}
                          <template v-if="media.duration_seconds"> · {{ media.duration_seconds }} 秒</template>
                          <template v-if="media.generation_mode"> · {{ mediaModeLabel(media.generation_mode) }}</template>
                        </small>
                      </span>
                    </div>
                    <a :href="media.media_url" :download="media.name" title="下载媒体"><Download :size="16" /></a>
                  </div>
                  <details class="agent-generated-media__prompt">
                    <summary><WandSparkles :size="14" /><span>生成提示词</span><small>{{ mediaSizeLabel(media.size_bytes) }}</small><ChevronDown :size="14" /></summary>
                    <div>
                      <p>{{ media.prompt }}</p>
                      <button type="button" @click="copyMediaPrompt(media)">
                        <Check v-if="copiedMessageId === `media-${media.id}`" :size="14" />
                        <Copy v-else :size="14" />
                        {{ copiedMessageId === `media-${media.id}` ? '已复制' : '复制提示词' }}
                      </button>
                    </div>
                  </details>
                </section>

                <AgentExecutionPanel
                  v-if="message.role === 'assistant' && executionSteps(message).length"
                  :steps="executionSteps(message)"
                  :expanded="executionRunExpanded(message.id)"
                  @toggle="toggleExecutionRun(message.id)"
                />

                <div class="agent-message__actions" :aria-label="message.role === 'user' ? '用户消息操作' : 'Agent 消息操作'">
                  <button type="button" :title="copiedMessageId === message.id ? '已复制' : '复制消息'" @click="copyMessage(message)">
                    <Check v-if="copiedMessageId === message.id" :size="14" />
                    <Copy v-else :size="14" />
                    <span>{{ copiedMessageId === message.id ? '已复制' : '复制' }}</span>
                  </button>
                  <button v-if="message.role === 'user'" type="button" title="放回输入框继续编辑" @click="editMessage(message)">
                    <PencilLine :size="14" /><span>继续编辑</span>
                  </button>
                </div>

                <section v-if="message.role === 'assistant' && fileChanges(message).length" class="agent-file-sync">
                  <button
                    class="agent-file-sync__trigger"
                    type="button"
                    :aria-expanded="fileRunExpanded(message.id)"
                    @click="toggleFileRun(message.id)"
                  >
                    <span class="agent-file-sync__icon"><Files :size="16" /></span>
                    <span>
                      <strong>{{ fileSyncTitle(message) }}</strong>
                      <small>
                        <template v-if="fileChangeCount(message, 'applied')">{{ fileChangeCount(message, 'applied') }} 项已应用</template>
                        <template v-if="fileChangeCount(message, 'conflict')"> · {{ fileChangeCount(message, 'conflict') }} 项冲突</template>
                        <template v-if="fileChangeCount(message, 'rejected')"> · {{ fileChangeCount(message, 'rejected') }} 项受限</template>
                      </small>
                    </span>
                    <ChevronDown class="agent-file-sync__chevron" :class="{ active: fileRunExpanded(message.id) }" :size="15" />
                  </button>
                  <div class="agent-file-sync__reveal" :class="{ active: fileRunExpanded(message.id) }">
                    <div>
                      <ul>
                        <li v-for="(change, index) in fileChanges(message)" :key="`${change.file_id || change.name}-${index}`" :class="`is-${change.status}`">
                          <span>
                            <ImagePlus v-if="change.operation === 'queue_asset_image_generation' || change.operation === 'queue_asset_prompt_generation'" :size="15" />
                            <FilePlus2 v-else-if="change.operation === 'create'" :size="15" />
                            <Trash2 v-else-if="change.operation === 'delete'" :size="15" />
                            <FilePenLine v-else :size="15" />
                          </span>
                          <span><strong>{{ change.name }}</strong><small>{{ fileChangeDetail(change) }}<template v-if="change.reason"> · {{ change.reason }}</template></small></span>
                          <CircleCheckBig v-if="change.status === 'applied'" :size="15" />
                          <TriangleAlert v-else :size="15" />
                        </li>
                      </ul>
                    </div>
                  </div>
                </section>
              </div>
                </article>
              </div>
            </div>

            <Transition name="agent-workflow-card">
              <DirectorAgentWorkflowCard
                v-if="scene === 'director' && directorWorkflow"
                :detail="directorWorkflow"
                :submitting-option="directorDecisionAction"
                @decide="submitDirectorDecision"
              />
            </Transition>

            <Transition name="agent-stream">
              <article v-if="sending" class="agent-message agent-message--assistant agent-message--thinking">
                <span class="agent-avatar agent-avatar--live"><Sparkles :size="15" /></span>
                <div class="agent-message__body agent-message__body--streaming">
                  <div class="agent-stream-output">
                    <div v-if="displayedStreamText" class="agent-message__content agent-message__content--streaming">
                      <div class="agent-markdown agent-stream-markdown" v-html="streamedMarkdown"></div>
                      <i class="agent-stream-caret" aria-hidden="true"></i>
                    </div>
                    <div
                      v-else-if="personalMode && ['image', 'video'].includes(currentRunMode)"
                      class="agent-media-progress"
                      :data-mode="currentRunMode"
                      role="status"
                      aria-live="polite"
                    >
                      <span><ImageIcon v-if="currentRunMode === 'image'" :size="17" /><Video v-else :size="17" /></span>
                      <div><strong>{{ currentRunMode === 'image' ? '正在生成图片' : '正在生成视频' }}</strong><small>{{ runStatusLabel }}</small></div>
                      <b>{{ trackedTask?.progress ?? 0 }}%</b>
                      <i><span :style="{ width: `${trackedTask?.progress ?? 0}%` }"></span></i>
                    </div>
                  </div>
                  <AgentExecutionPanel
                    :steps="liveExecutionSteps"
                    :active="sending"
                    :expanded="liveExecutionExpanded"
                    :output-length="displayedStreamText.length"
                    @toggle="liveExecutionExpanded = !liveExecutionExpanded"
                  />
                </div>
              </article>
            </Transition>
          </div>

          <Transition name="agent-stream">
            <button v-if="!stickToLatest" class="agent-scroll-latest" type="button" title="回到最新消息" @click="scrollToLatest()">
              <ArrowDown :size="17" /><span>回到最新</span>
            </button>
          </Transition>
        </div>

        <div v-else class="agent-empty-guide">
          <span class="agent-empty-guide__icon"><Sparkles :size="20" /></span>
          <div>
            <strong>{{ emptyGuideTitle }}</strong>
            <p>{{ emptyGuideDescription }}</p>
          </div>
          <div v-if="scene === 'director'" class="agent-starters" aria-label="导演创作建议">
            <button type="button" @click="useStarter('先分析本章，并给出适合 AI 视频短剧的改编策略。')">
              <MessageSquareText :size="15" />分析与改编
            </button>
            <button type="button" @click="useStarter('基于本章原文创作首版剧本，完成后自动审核并告诉我需要决定的问题。')">
              <Clapperboard :size="15" />创作首版剧本
            </button>
            <button type="button" @click="useStarter('检查当前生效剧本；若审核通过，请继续完成资产提取。')">
              <Check :size="15" />审核并提取资产
            </button>
            <button type="button" @click="useStarter('检查本章资产是否齐备，满足条件后继续制作并审核分镜。')">
              <Sparkles :size="15" />继续制作分镜
            </button>
          </div>
          <div v-else-if="personalMode" class="agent-starters agent-starters--personal" :aria-label="`${currentPersonalMode.label}建议`">
            <button v-for="starter in personalStarters" :key="starter.label" type="button" @click="useStarter(starter.prompt)">
              <component :is="starter.icon" :size="15" />{{ starter.label }}
            </button>
          </div>
          <div v-else class="agent-starters" aria-label="快捷创作">
            <button type="button" @click="useStarter('根据当前项目，先生成一个三幕式故事骨架。')">
              <MessageSquareText :size="15" />生成故事骨架
            </button>
          </div>
        </div>

        <div class="agent-composer">
          <Transition name="agent-command">
            <section v-if="skillCommandOpen" class="agent-skill-command" aria-label="可用 Skills">
              <header>
                <span><Search :size="15" /></span>
                <div><strong>调用 Skill</strong><small>{{ slashSkillQuery ? `搜索“${slashSkillQuery}”` : '选择一个或多个能力绑定到本轮' }}</small></div>
                <kbd>/</kbd>
              </header>
              <div v-if="filteredSlashSkills.length" class="agent-skill-command__list">
                <button
                  v-for="(skill, index) in filteredSlashSkills"
                  :key="skill.id"
                  type="button"
                  :class="{ active: index === skillCommandIndex, selected: selectedSkillIds.includes(skill.id) }"
                  @mouseenter="skillCommandIndex = index"
                  @mousedown.prevent="selectSlashSkill(skill)"
                >
                  <span><BrainCircuit :size="16" /></span>
                  <span><strong>{{ skill.name }}</strong><small>{{ skill.description }}</small></span>
                  <span><Check v-if="selectedSkillIds.includes(skill.id)" :size="15" /><Plus v-else :size="15" /></span>
                </button>
              </div>
              <div v-else class="agent-skill-command__empty"><Search :size="18" /><span>没有匹配的已启用 Skill</span></div>
            </section>
          </Transition>

          <TransitionGroup v-if="personalMode && selectedUserSkills.length" name="agent-skill-chip" tag="div" class="agent-selected-skills">
            <span v-for="skill in selectedUserSkills" :key="skill.id">
              <BrainCircuit :size="13" />{{ skill.name }}
              <button type="button" :title="`移除 ${skill.name}`" @click="removeSelectedSkill(skill.id)"><X :size="12" /></button>
            </span>
          </TransitionGroup>

          <TransitionGroup v-if="pendingAttachments.length" name="agent-attachment" tag="div" class="agent-attachment-tray">
            <figure v-for="attachment in pendingAttachments" :key="attachment.id">
              <img :src="attachment.media_url" :alt="attachment.name" />
              <button type="button" title="移除图片" @click="removePendingAttachment(attachment)"><X :size="14" /></button>
            </figure>
          </TransitionGroup>
          <textarea
            ref="textarea"
            v-model="draft"
            rows="1"
            maxlength="200000"
            :disabled="(!personalMode && !selectedProject) || !options.agents.length"
            :placeholder="personalMode ? personalPlaceholder : selectedProject ? `描述想法、粘贴原文，或分析《${selectedProject.name}》` : '等待项目上下文加载'"
            aria-label="发送给创作 Agent"
            @input="resizeTextarea"
            @paste="pasteAttachments"
            @keydown="handleComposerKeydown"
          ></textarea>

          <div v-if="personalMode && personalAgentMode === 'image'" class="agent-media-options">
            <span><SlidersHorizontal :size="14" />图片参数</span>
            <div class="agent-option-group" aria-label="图片比例">
              <button v-for="ratio in ['1:1', '16:9', '9:16']" :key="ratio" type="button" :aria-pressed="imageAspectRatio === ratio" @click="imageAspectRatio = ratio">{{ ratio }}</button>
            </div>
            <div class="agent-option-group" aria-label="图片分辨率">
              <button v-for="resolution in ['1K', '2K', '4K']" :key="resolution" type="button" :aria-pressed="imageResolution === resolution" @click="imageResolution = resolution">{{ resolution }}</button>
            </div>
          </div>
          <div v-else-if="personalMode && personalAgentMode === 'video'" class="agent-media-options agent-media-options--video">
            <span><SlidersHorizontal :size="14" />视频参数</span>
            <div class="agent-option-group" aria-label="视频比例">
              <button v-for="ratio in ['16:9', '9:16', '1:1']" :key="ratio" type="button" :aria-pressed="videoAspectRatio === ratio" @click="videoAspectRatio = ratio">{{ ratio }}</button>
            </div>
            <div class="agent-option-group" aria-label="视频分辨率">
              <button v-for="resolution in ['720p', '1080p']" :key="resolution" type="button" :aria-pressed="videoResolution === resolution" @click="videoResolution = resolution">{{ resolution }}</button>
            </div>
            <div class="agent-option-group" aria-label="视频时长">
              <button v-for="duration in [5, 10]" :key="duration" type="button" :aria-pressed="videoDuration === duration" @click="videoDuration = duration">{{ duration }}s</button>
            </div>
          </div>

          <div class="agent-composer__actions">
            <input ref="fileInput" class="sr-only" type="file" accept="image/jpeg,image/png,image/webp" multiple @change="uploadAttachments" />
            <button
              class="agent-attach-button"
              type="button"
              :disabled="sending || uploadingAttachments || pendingAttachments.length >= maxAttachmentCount"
              :title="pendingAttachments.length >= maxAttachmentCount ? `最多添加 ${maxAttachmentCount} 张图片` : '上传图片'"
              @click="openAttachmentPicker"
            >
              <LoaderCircle v-if="uploadingAttachments" class="spin" :size="18" />
              <ImagePlus v-else :size="18" />
            </button>
            <button
              class="agent-send-button"
              :class="[
                { 'is-stopping': sending },
                personalMode && !sending ? `agent-send-button--${personalAgentMode}` : '',
              ]"
              type="button"
              :disabled="sending ? !trackedTask || cancelling : !canSend"
              :title="sending ? '停止生成' : '发送'"
              @click="sending ? stopCurrentRun() : sendMessage()"
            >
              <span class="agent-send-button__icons" :class="{ active: sending }">
                <Square class="agent-send-button__stop" :size="16" fill="currentColor" />
                <Send class="agent-send-button__send" :size="18" />
              </span>
              <span>{{ sending ? (cancelling ? '停止中' : '停止') : (personalMode ? sendButtonLabel : '发送') }}</span>
            </button>
          </div>
        </div>
      </div>
    </div>
    <BaseDialog
      :open="Boolean(deleteSessionTarget)"
      title="删除当前对话"
      description="该操作会清除当前对话的全部消息和对话图片，且无法恢复"
      @update:open="!$event && !deletingSession && (deleteSessionTarget = null)"
    >
      <div class="danger-confirm">
        <span><Trash2 :size="22" /></span>
        <div>
          <strong>{{ deleteSessionTarget?.title }}</strong>
          <p>任务与积分流水仍会作为审计记录保留。</p>
        </div>
      </div>
      <template #footer>
        <button class="button button--ghost" type="button" :disabled="deletingSession" @click="deleteSessionTarget = null">取消</button>
        <button class="button button--danger" type="button" :disabled="deletingSession" @click="deleteCurrentSession">
          <LoaderCircle v-if="deletingSession" class="spin" :size="17" />
          <Trash2 v-else :size="17" />确认删除
        </button>
      </template>
    </BaseDialog>
    </section>
  </Teleport>
</template>
