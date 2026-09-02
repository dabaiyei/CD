<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import {
  Activity,
  Bot,
  BookMarked,
  BookOpen,
  Check,
  CircleAlert,
  Code2,
  Coins,
  Cpu,
  Download,
  Eye,
  FileCheck2,
  FileCode2,
  Files,
  Film,
  Fingerprint,
  Gauge,
  ImagePlus,
  KeyRound,
  Layers3,
  ListFilter,
  LockKeyhole,
  LoaderCircle,
  LogIn,
  Minus,
  Network,
  Pencil,
  Plus,
  RefreshCw,
  Save,
  Search,
  ServerCog,
  Settings2,
  ShieldCheck,
  Sparkles,
  TestTube2,
  Trash2,
  UsersRound,
  Volume2,
} from 'lucide-vue-next'

import AdminUsersPanel from '@/components/AdminUsersPanel.vue'
import BaseDialog from '@/components/BaseDialog.vue'
import SkillTree from '@/components/SkillTree.vue'
import UiSelect from '@/components/UiSelect.vue'
import VideoCapabilityEditor from '@/components/VideoCapabilityEditor.vue'
import { api } from '@/lib/api'
import { renderMarkdown } from '@/lib/markdown'
import { useToastStore } from '@/stores/toast'
import type {
  AgentProfile,
  AIModel,
  DiscoveredModel,
  Handbook,
  HandbookManifest,
  HandbookPackage,
  HandbookSkillFile,
  HandbookType,
  ModelType,
  Project,
  PricingRule,
  PromptTemplate,
  Provider,
  ProviderAdapterConfig,
  ProviderCredentialField,
  ProviderType,
  Readiness,
  SecurityEvent,
  SecurityEventPage,
  SkillNode,
  VideoModelCapabilities,
} from '@/types'

type DialogKind = 'provider' | 'model' | 'pricing' | 'agent' | 'prompt' | 'handbook' | null
type DiscoveryRow = DiscoveredModel & { selected: boolean; model_type: ModelType }
type DeleteTarget = { kind: 'provider' | 'model'; id: string; name: string }

const route = useRoute()
const toast = useToastStore()
const loading = ref(true)
const saving = ref(false)
const testingProviderId = ref<string | null>(null)
const testingModelId = ref<string | null>(null)
const installingAutoDlPreset = ref(false)
const settingDefaultType = ref<ModelType | null>(null)
const dialog = ref<DialogKind>(null)
const discoveryProvider = ref<Provider | null>(null)
const discoveryRows = ref<DiscoveryRow[]>([])
const discoverySearch = ref('')
const discovering = ref(false)
const importing = ref(false)
const deleteTarget = ref<DeleteTarget | null>(null)
const deleting = ref(false)
const adminTabs = ref<HTMLElement | null>(null)
const editingRequiredDefault = ref(false)
const securityLoading = ref(false)
const securityLoadingMore = ref(false)
const securityEvents = ref<SecurityEvent[]>([])
const securityNextBefore = ref<string | null>(null)
const securityNextBeforeId = ref<string | null>(null)
const securityType = ref('all')
const securityOutcome = ref<'all' | 'success' | 'failed'>('all')
let securityRequestSequence = 0

const readiness = ref<Readiness | null>(null)
const providers = ref<Provider[]>([])
const activeProviderId = ref('')
const models = ref<AIModel[]>([])
const agents = ref<AgentProfile[]>([])
const prompts = ref<PromptTemplate[]>([])
const handbooks = ref<Handbook[]>([])
const handbookManifests = ref<HandbookManifest[]>([])
const handbookFiles = ref<HandbookSkillFile[]>([])
const activeHandbookFileKey = ref('')
const loadingHandbookPackage = ref(false)
const projects = ref<Project[]>([])
const pricingRules = ref<PricingRule[]>([])
const skillTree = ref<SkillNode[]>([])
const selectedSkill = ref('')
const skillContent = ref('')
const skillDirty = ref(false)
const handbookCoverInput = ref<HTMLInputElement | null>(null)
const pendingHandbookCover = ref<File | null>(null)
const pendingHandbookCoverPreview = ref<string | null>(null)

const validSections = ['overview', 'users', 'models', 'pricing', 'agents', 'prompts', 'handbooks', 'skills', 'security'] as const
const section = computed(() => {
  const value = String(route.params.section || 'overview')
  return validSections.includes(value as (typeof validSections)[number]) ? value : 'overview'
})

const tabs = [
  { id: 'overview', label: '总览', icon: Gauge },
  { id: 'users', label: '用户与积分', icon: UsersRound },
  { id: 'models', label: '模型平台', icon: ServerCog },
  { id: 'pricing', label: '计费规则', icon: Coins },
  { id: 'agents', label: 'Agent', icon: Bot },
  { id: 'prompts', label: '提示词', icon: FileCode2 },
  { id: 'handbooks', label: '创作手册', icon: BookOpen },
  { id: 'skills', label: 'Skills', icon: Sparkles },
  { id: 'security', label: '安全审计', icon: ShieldCheck },
]

const providerForm = reactive({
  id: '',
  code: '',
  name: '',
  provider_type: 'openai_compatible' as ProviderType,
  base_url: '',
  api_key: '',
  clear_api_key: false,
  credentials: {} as Record<string, string>,
  adapter_json: '',
  adapter_expanded: false,
  max_concurrency: 2,
  enabled: true,
})

function defaultVideoCapabilities(): VideoModelCapabilities {
  return {
    schema_version: 1,
    generation_modes: ['text_to_video'],
    reference_limits: {
      image: { enabled: false, min_count: 0, max_count: 0 },
      video: { enabled: false, min_count: 0, max_count: 0 },
      audio: { enabled: false, min_count: 0, max_count: 0 },
    },
    audio_policy: 'optional',
    duration_resolution_map: [{ durations: [5, 10], resolutions: ['720p', '1080p'] }],
    aspect_ratios: ['16:9', '9:16'],
    prompt_languages: ['zh-CN'],
    negative_prompt_supported: false,
    asynchronous: true,
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function cloneJsonRecord(value: unknown): Record<string, unknown> {
  if (!isRecord(value)) return {}
  try {
    return JSON.parse(JSON.stringify(value)) as Record<string, unknown>
  } catch {
    return { ...value }
  }
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.filter((item): item is string => typeof item === 'string' && item.trim().length > 0)
}

function normalizeProviderRow(provider: Provider): Provider {
  const maxConcurrency = Number(provider.max_concurrency)
  return {
    ...provider,
    extra_headers: isRecord(provider.extra_headers)
      ? Object.fromEntries(Object.entries(provider.extra_headers).filter((entry): entry is [string, string] => typeof entry[1] === 'string'))
      : {},
    adapter_config: cloneJsonRecord(provider.adapter_config),
    configured_credentials: stringList(provider.configured_credentials),
    max_concurrency: Number.isFinite(maxConcurrency) && maxConcurrency > 0 ? maxConcurrency : 1,
  }
}

function normalizeModelRow(model: AIModel): AIModel {
  return { ...model, capabilities: cloneJsonRecord(model.capabilities) }
}

const modelForm = reactive({
  id: '',
  provider_id: '',
  model_id: '',
  name: '',
  model_type: 'text' as ModelType,
  capabilities: defaultVideoCapabilities(),
  enabled: true,
  is_default: false,
})
const agentForm = reactive({
  id: '',
  kind: 'screenplay' as 'screenplay' | 'general',
  name: '',
  description: '',
  system_prompt: '',
  text_model_id: '',
  routing_priority: 0,
  memory_enabled: true,
  enabled: true,
})
const agentConfigBase = ref<Record<string, unknown>>({})
const promptForm = reactive({ id: '', code: '', name: '', description: '', content: '', enabled: true })
const handbookForm = reactive({
  id: '',
  handbook_type: 'visual' as HandbookType,
  name: '',
  description: '',
  cover_url: '',
  enabled: true,
})
const pricingForm = reactive({
  task_type: '',
  name: '',
  description: '',
  unit_label: '',
  unit_cost: '0.00',
  version: 1,
})
const dialogTitle = computed(() => {
  if (dialog.value === 'provider') return providerForm.id ? '编辑模型平台' : '新增模型平台'
  if (dialog.value === 'model') return modelForm.id ? '编辑模型' : '新增模型'
  if (dialog.value === 'pricing') return '编辑计费规则'
  if (dialog.value === 'agent') return 'Agent 配置'
  if (dialog.value === 'prompt') return '提示词配置'
  return '创作手册'
})
const handbookCoverPreview = computed(
  () => pendingHandbookCoverPreview.value || handbookForm.cover_url || '/covers/login-studio.jpg',
)
const activeHandbookFile = computed(() => (
  handbookFiles.value.find((item) => item.key === activeHandbookFileKey.value) ?? handbookFiles.value[0] ?? null
))
const handbookPreview = computed(() => renderMarkdown(activeHandbookFile.value?.content ?? ''))
const validHandbookFileCount = computed(() => handbookFiles.value.filter((item) => item.content.trim()).length)
const handbookFilesComplete = computed(() => (
  handbookFiles.value.length > 0 && validHandbookFileCount.value === handbookFiles.value.length
))
const editingProviderHasEnabledModels = computed(() => Boolean(
  providerForm.id
  && providerForm.enabled
  && models.value.some((model) => model.provider_id === providerForm.id && model.enabled),
))
const editingHandbookUsedByProjects = computed(() => Boolean(
  handbookForm.id
  && handbookForm.enabled
  && projects.value.some((project) => (
    handbookForm.handbook_type === 'visual'
      ? project.visual_handbook_id === handbookForm.id
      : project.director_handbook_id === handbookForm.id
  )),
))

const modelTypeLabel: Record<ModelType, string> = { text: '文本', image: '图片', video: '视频', tts: 'TTS' }
const requiredModelTypes = new Set<ModelType>(['text', 'image', 'video'])
const modelTypeIcon = { text: Cpu, image: Sparkles, video: Activity, tts: Volume2 }
const modelTypeOptions = (Object.keys(modelTypeLabel) as ModelType[]).map((value) => ({
  value,
  label: `${modelTypeLabel[value]}模型`,
  description: value === 'text' ? '对话与结构化文本' : value === 'image' ? '图片与视觉资产' : value === 'video' ? '动态画面生成' : '语音合成',
  icon: modelTypeIcon[value],
}))
const providerTypeOptions = [
  { value: 'sub2api', label: 'Sub2API', description: 'Sub2API 标准平台', icon: Network },
  { value: 'newapi', label: 'New API', description: 'New API 聚合平台', icon: ServerCog },
  { value: 'openai_compatible', label: 'OpenAI 兼容', description: '兼容 /v1/models 协议', icon: Cpu },
  { value: 'custom', label: '自定义平台', description: '自定义兼容网关', icon: Sparkles },
]
const activeProvider = computed(() => (
  providers.value.find((item) => item.id === activeProviderId.value)
  ?? providers.value.find((item) => item.enabled)
  ?? providers.value[0]
  ?? null
))
const activeProviderModels = computed(() => models.value.filter((item) => item.provider_id === activeProvider.value?.id))
const autoDlPresetInstalled = computed(() => providers.value.some((item) => item.code === 'autodl-minimax-h3'))
const activeProviderCredentialCount = computed(() => activeProvider.value?.configured_credentials.length ?? 0)
const activeProviderHasAdapter = computed(() => Object.keys(activeProvider.value?.adapter_config ?? {}).length > 0)
const videoCapabilitySummaries = computed<Record<string, string[]>>(() => Object.fromEntries(
  models.value.map((model) => [model.id, videoCapabilitySummary(model)]),
))
const providerAdapterState = computed<{ config: ProviderAdapterConfig | null; error: string }>(() => {
  const source = providerForm.adapter_json.trim()
  if (!source) return { config: { schema_version: 1, credential_fields: [] }, error: '' }
  try {
    const config = JSON.parse(source) as ProviderAdapterConfig
    if (config.schema_version !== 1 || !Array.isArray(config.credential_fields)) {
      return { config: null, error: '适配协议必须包含 schema_version: 1 和 credential_fields 数组' }
    }
    return { config, error: '' }
  } catch {
    return { config: null, error: 'JSON 格式不正确，请检查括号、引号和逗号' }
  }
})
const providerCredentialFields = computed<ProviderCredentialField[]>(() => providerAdapterState.value.config?.credential_fields ?? [])

function providerForModel(model: AIModel | undefined): Provider | undefined {
  return model ? providers.value.find((provider) => provider.id === model.provider_id) : undefined
}

function defaultModelFor(modelType: ModelType): AIModel | undefined {
  return models.value.find((model) => model.model_type === modelType && model.is_default)
}

function defaultModelOptions(modelType: ModelType) {
  return models.value
    .filter((model) => (
      model.model_type === modelType
      && model.enabled
      && providerForModel(model)?.enabled
    ))
    .map((model) => ({
      value: model.id,
      label: model.name,
      description: `${providerForModel(model)?.name ?? '未知平台'} · ${model.model_id}`,
      icon: modelTypeIcon[modelType],
    }))
}

const customVideoAdapterTemplate: ProviderAdapterConfig = {
  schema_version: 1,
  credential_fields: [
    {
      key: 'apiKey',
      label: '服务商 Token',
      input_type: 'password',
      required: true,
      placeholder: '输入上游服务 Token',
      help_text: '仅加密保存在服务端，不会返回到浏览器',
    },
  ],
  connectivity: {
    method: 'GET',
    path: 'health',
    headers: { Authorization: '{{credentials.apiKey}}' },
    query: {},
    body: null,
    assertions: [],
  },
  video: {
    create: {
      method: 'POST',
      path: 'api/v1/comfyui/comfyui_workflow/{{model}}',
      headers: { Authorization: '{{credentials.apiKey}}', 'Content-Type': 'application/json' },
      query: {},
      body: { prompt: '{{prompt}}', duration: '{{duration}}', resolution: '{{resolution}}' },
      assertions: [{ path: 'code', accepted_values: ['Success'], message: '视频生成任务创建失败' }],
    },
    poll: {
      method: 'GET',
      path: 'api/v1/comfyui/comfyui_workflow/result/{{job_id}}',
      headers: { Authorization: '{{credentials.apiKey}}' },
      query: {},
      body: null,
      assertions: [{ path: 'code', accepted_values: ['Success'], message: '视频任务查询失败' }],
    },
    response: {
      task_id_path: 'data.task_id',
      status_path: 'data.status',
      result_url_path: 'data.results[?type=video].url',
      result_base64_path: '',
      error_path: 'msg',
      success_values: ['SUCCESS'],
      pending_values: ['QUEUED', 'RUNNING', 'PROCESSING'],
      failed_values: ['FAILED', 'ERROR', 'CANCELLED'],
    },
    references: [{ media_type: 'image', strategy: 'indexed_fields', field: 'ref_image_', start_index: 0, source: 'data_uri' }],
    poll_interval_seconds: 5,
    poll_timeout_seconds: 1800,
  },
}
const adapterVariables = ['{{model}}', '{{prompt}}', '{{duration}}', '{{resolution}}', '{{job_id}}', '{{credentials.key}}']
const providerOptions = computed(() => providers.value.map((item) => ({ value: item.id, label: item.name, description: item.base_url, icon: ServerCog })))
const textModelOptions = computed(() => models.value.filter((item) => item.model_type === 'text').map((item) => ({ value: item.id, label: item.name, description: item.model_id, icon: Cpu })))
const agentKindOptions = [
  { value: 'screenplay', label: '剧本 Agent', description: '故事骨架、改编策略与剧本创作', icon: Bot },
  { value: 'general', label: '通用 AI', description: '事件、资产与台词等辅助能力', icon: Sparkles },
]
const handbookTypeOptions = [
  { value: 'visual', label: '视觉手册', description: '画风与视觉资产生成约束', icon: Sparkles },
  { value: 'director', label: '导演手册', description: '叙事规划与分镜技法', icon: BookOpen },
]
const securityTypeOptions = [
  { value: 'all', label: '全部事件', description: '显示所有登录安全事件', icon: ListFilter },
  { value: 'login_succeeded', label: '登录成功', description: '通过身份校验并签发会话', icon: LogIn },
  { value: 'login_failed', label: '登录失败', description: '租户、账号或密码校验失败', icon: CircleAlert },
  { value: 'login_locked', label: '账号锁定', description: '持续失败触发身份级锁定', icon: LockKeyhole },
  { value: 'login_rate_limited', label: '访问限流', description: '请求频率超过安全阈值', icon: Activity },
]
const securityEventMeta: Record<string, { label: string; description: string; icon: typeof LogIn; tone: string }> = {
  login_succeeded: { label: '登录成功', description: '身份校验通过', icon: LogIn, tone: 'success' },
  login_failed: { label: '登录失败', description: '凭据校验未通过', icon: CircleAlert, tone: 'danger' },
  login_locked: { label: '账号锁定', description: '身份已被临时锁定', icon: LockKeyhole, tone: 'warning' },
  login_rate_limited: { label: '访问限流', description: '请求频率超过阈值', icon: Activity, tone: 'warning' },
}
const securitySuccessCount = computed(() => securityEvents.value.filter((item) => item.success).length)
const securityFailureCount = computed(() => securityEvents.value.length - securitySuccessCount.value)
const filteredDiscoveryRows = computed(() => {
  const keyword = discoverySearch.value.trim().toLowerCase()
  if (!keyword) return discoveryRows.value
  return discoveryRows.value.filter((item) => `${item.name} ${item.model_id} ${item.owned_by || ''}`.toLowerCase().includes(keyword))
})
const selectableDiscoveryRows = computed(() => discoveryRows.value.filter((item) => !item.is_imported))
const selectedDiscoveryCount = computed(() => selectableDiscoveryRows.value.filter((item) => item.selected).length)
const allDiscoverySelected = computed(() => selectableDiscoveryRows.value.length > 0 && selectedDiscoveryCount.value === selectableDiscoveryRows.value.length)

onMounted(async () => {
  await loadAll()
  if (section.value === 'security') await loadSecurityEvents(true)
  await revealActiveTab(false)
})
watch(section, async (value) => {
  window.scrollTo({ top: 0, behavior: 'smooth' })
  if (value === 'security' && securityEvents.value.length === 0) await loadSecurityEvents(true)
  await revealActiveTab(true)
})
watch([securityType, securityOutcome], async () => {
  if (section.value === 'security') await loadSecurityEvents(true)
})
watch(
  () => modelForm.enabled,
  (enabled) => {
    if (!enabled && !editingRequiredDefault.value) modelForm.is_default = false
  },
)
watch(providers, (items) => {
  if (!items.some((item) => item.id === activeProviderId.value)) {
    activeProviderId.value = items.find((item) => item.enabled)?.id ?? items[0]?.id ?? ''
  }
})
watch(
  () => modelForm.is_default,
  (isDefault) => {
    if (isDefault) modelForm.enabled = true
  },
)
watch(
  () => handbookForm.handbook_type,
  (value) => {
    if (dialog.value === 'handbook' && !handbookForm.id) initializeHandbookFiles(value)
  },
)

function securityQuery(before?: string | null, beforeId?: string | null): string {
  const params = new URLSearchParams({ limit: '30' })
  if (securityType.value !== 'all') params.set('event_type', securityType.value)
  if (securityOutcome.value !== 'all') params.set('success', String(securityOutcome.value === 'success'))
  if (before && beforeId) {
    params.set('before', before)
    params.set('before_id', beforeId)
  }
  return `/admin/security-events?${params.toString()}`
}

async function revealActiveTab(smooth: boolean): Promise<void> {
  await nextTick()
  adminTabs.value?.querySelector<HTMLElement>('a.active')?.scrollIntoView({
    behavior: smooth ? 'smooth' : 'instant',
    block: 'nearest',
    inline: 'center',
  })
}

async function loadSecurityEvents(reset = false): Promise<void> {
  if (!reset && (securityLoading.value || securityLoadingMore.value)) return
  const requestId = ++securityRequestSequence
  if (reset) securityLoading.value = true
  else securityLoadingMore.value = true
  try {
    const page = await api<SecurityEventPage>(
      securityQuery(reset ? null : securityNextBefore.value, reset ? null : securityNextBeforeId.value),
    )
    if (requestId !== securityRequestSequence) return
    securityEvents.value = reset ? page.items : [...securityEvents.value, ...page.items]
    securityNextBefore.value = page.next_before
    securityNextBeforeId.value = page.next_before_id
  } catch (error) {
    if (requestId !== securityRequestSequence) return
    toast.show('安全事件加载失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    if (requestId === securityRequestSequence) {
      securityLoading.value = false
      securityLoadingMore.value = false
    }
  }
}

function securityMeta(event: SecurityEvent) {
  return securityEventMeta[event.event_type] ?? {
    label: event.event_type,
    description: '系统安全事件',
    icon: ShieldCheck,
    tone: event.success ? 'success' : 'danger',
  }
}

function shortHash(value: string): string {
  return `${value.slice(0, 8)}...${value.slice(-6)}`
}

function formatSecurityTime(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(new Date(value))
}

function securityDetail(event: SecurityEvent): string {
  const retryAfter = event.event_metadata.retry_after_seconds
  if (typeof retryAfter === 'number') return `${retryAfter} 秒后可重试`
  if (event.event_metadata.locked === true) return '本次失败已触发临时锁定'
  return securityMeta(event).description
}

function formatCredits(value: string): string {
  return Number(value).toFixed(2)
}

async function loadAll(): Promise<void> {
  loading.value = true
  try {
    const [
      readyRows,
      providerRows,
      modelRows,
      pricingRows,
      agentRows,
      promptRows,
      handbookRows,
      manifestRows,
      projectRows,
      skillRows,
    ] = await Promise.all([
      api<Readiness>('/admin/readiness'),
      api<Provider[]>('/admin/providers'),
      api<AIModel[]>('/admin/models'),
      api<PricingRule[]>('/admin/pricing-rules'),
      api<AgentProfile[]>('/admin/agents'),
      api<PromptTemplate[]>('/admin/prompts'),
      api<Handbook[]>('/admin/handbooks'),
      api<HandbookManifest[]>('/admin/handbooks/manifests'),
      api<Project[]>('/projects'),
      api<SkillNode[]>('/admin/skills/tree'),
    ])
    readiness.value = readyRows
    providers.value = providerRows.map(normalizeProviderRow)
    models.value = modelRows.map(normalizeModelRow)
    pricingRules.value = pricingRows
    agents.value = agentRows
    prompts.value = promptRows
    handbooks.value = handbookRows
    handbookManifests.value = manifestRows
    projects.value = projectRows
    skillTree.value = skillRows
  } catch (error) {
    toast.show('管理数据加载失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    loading.value = false
  }
}

function openProvider(provider?: Provider): void {
  const adapterConfig = provider?.adapter_config && Object.keys(provider.adapter_config).length
    ? provider.adapter_config
    : { schema_version: 1, credential_fields: [] }
  Object.assign(providerForm, {
    id: provider?.id ?? '',
    code: provider?.code ?? '',
    name: provider?.name ?? '',
    provider_type: provider?.provider_type ?? 'openai_compatible',
    base_url: provider?.base_url ?? '',
    api_key: '',
    clear_api_key: false,
    credentials: Object.fromEntries((provider?.configured_credentials ?? []).map((key) => [key, ''])),
    adapter_json: JSON.stringify(adapterConfig, null, 2),
    adapter_expanded: provider?.provider_type === 'custom' || Boolean(provider && Object.keys(provider.adapter_config).length),
    max_concurrency: provider?.max_concurrency ?? 2,
    enabled: provider?.enabled ?? true,
  })
  dialog.value = 'provider'
}

function normalizedVideoCapabilities(value: unknown): VideoModelCapabilities {
  const defaults = defaultVideoCapabilities()
  const capabilities = cloneJsonRecord(value)
  const supportedModes = new Set([
    'text_to_video',
    'first_frame',
    'first_last_frame',
    'last_frame',
    'full_reference',
    'multi_shot',
  ])
  const modes = stringList(capabilities.generation_modes)
    .filter((item): item is VideoModelCapabilities['generation_modes'][number] => supportedModes.has(item))

  const referenceLimits = isRecord(capabilities.reference_limits) ? capabilities.reference_limits : {}
  const normalizeLimit = (mediaType: 'image' | 'video' | 'audio') => {
    const fallback = defaults.reference_limits[mediaType]
    const source = isRecord(referenceLimits[mediaType]) ? referenceLimits[mediaType] : {}
    const minCount = Number(source.min_count)
    const maxCount = Number(source.max_count)
    const enabled = typeof source.enabled === 'boolean' ? source.enabled : fallback.enabled
    const normalizedMin = enabled && Number.isFinite(minCount) ? Math.max(0, Math.trunc(minCount)) : 0
    const normalizedMax = enabled && Number.isFinite(maxCount) ? Math.max(normalizedMin, Math.trunc(maxCount)) : 0
    return { enabled, min_count: normalizedMin, max_count: normalizedMax }
  }

  const durations = Array.isArray(capabilities.durations) ? capabilities.durations.filter((item): item is number => typeof item === 'number') : [5, 10]
  const resolutions = Array.isArray(capabilities.resolutions) ? capabilities.resolutions.filter((item): item is string => typeof item === 'string') : ['720p', '1080p']
  const durationMap = Array.isArray(capabilities.duration_resolution_map)
    ? capabilities.duration_resolution_map.flatMap((item) => {
      if (!isRecord(item)) return []
      const itemDurations = Array.isArray(item.durations)
        ? item.durations.filter((duration): duration is number => typeof duration === 'number' && Number.isFinite(duration) && duration > 0)
        : []
      const itemResolutions = stringList(item.resolutions)
      return itemDurations.length && itemResolutions.length
        ? [{ durations: itemDurations, resolutions: itemResolutions }]
        : []
    })
    : []
  const audioPolicy = capabilities.audio_policy === 'required' || capabilities.audio_policy === 'disabled'
    ? capabilities.audio_policy
    : 'optional'

  return {
    ...capabilities,
    schema_version: 1,
    generation_modes: modes.length ? modes : defaults.generation_modes,
    reference_limits: {
      image: normalizeLimit('image'),
      video: normalizeLimit('video'),
      audio: normalizeLimit('audio'),
    },
    audio_policy: audioPolicy,
    duration_resolution_map: durationMap.length
      ? durationMap
      : [{ durations: durations.length ? durations : [5, 10], resolutions: resolutions.length ? resolutions : ['720p', '1080p'] }],
    aspect_ratios: stringList(capabilities.aspect_ratios).length ? stringList(capabilities.aspect_ratios) : defaults.aspect_ratios,
    prompt_languages: stringList(capabilities.prompt_languages).length ? stringList(capabilities.prompt_languages) : defaults.prompt_languages,
    negative_prompt_supported: capabilities.negative_prompt_supported === true,
    asynchronous: capabilities.asynchronous !== false,
  }
}

function openModel(model?: AIModel, providerId?: string, modelType?: ModelType): void {
  editingRequiredDefault.value = Boolean(
    model?.is_default && requiredModelTypes.has(model.model_type),
  )
  Object.assign(modelForm, {
    id: model?.id ?? '',
    provider_id: model?.provider_id ?? providerId ?? activeProvider.value?.id ?? providers.value[0]?.id ?? '',
    model_id: model?.model_id ?? '',
    name: model?.name ?? '',
    model_type: model?.model_type ?? modelType ?? 'text',
    capabilities: normalizedVideoCapabilities(model?.capabilities ?? {}),
    enabled: model?.enabled ?? true,
    is_default: model?.is_default ?? false,
  })
  dialog.value = 'model'
}

async function setDefaultModel(modelType: ModelType, modelId: string): Promise<void> {
  const model = models.value.find((item) => item.id === modelId && item.model_type === modelType)
  if (!model || model.is_default || settingDefaultType.value) return
  settingDefaultType.value = modelType
  try {
    await api(`/admin/models/${model.id}`, {
      method: 'PATCH',
      body: JSON.stringify({ is_default: true }),
    })
    await loadAll()
    toast.show(`默认${modelTypeLabel[modelType]}模型已切换`, {
      message: `${model.name} 将供未单独指定模型的功能使用`,
      tone: 'success',
    })
  } catch (error) {
    toast.show('默认模型切换失败', {
      message: error instanceof Error ? error.message : undefined,
      tone: 'error',
    })
  } finally {
    settingDefaultType.value = null
  }
}

function useCustomAdapterTemplate(): void {
  providerForm.adapter_json = JSON.stringify(customVideoAdapterTemplate, null, 2)
  providerForm.adapter_expanded = true
  providerForm.provider_type = 'custom'
  providerForm.credentials = { apiKey: '' }
}

async function installAutoDlPreset(): Promise<void> {
  if (installingAutoDlPreset.value || autoDlPresetInstalled.value) return
  installingAutoDlPreset.value = true
  try {
    const result = await api<{ provider: Provider; model: AIModel }>(
      '/admin/provider-presets/autodl-minimax-h3/install',
      { method: 'POST' },
    )
    await loadAll()
    activeProviderId.value = result.provider.id
    toast.show('AutoDL MiniMax H3 已添加', {
      message: '供应商和视频模型暂未启用，请先填写真实 Base URL 与 ComfyUI Token。',
      tone: 'success',
    })
  } catch (error) {
    toast.show('AutoDL MiniMax H3 添加失败', {
      message: error instanceof Error ? error.message : undefined,
      tone: 'error',
    })
  } finally {
    installingAutoDlPreset.value = false
  }
}

function providerModelCount(providerId: string): number {
  return models.value.filter((item) => item.provider_id === providerId).length
}

function credentialPlaceholder(credential: ProviderCredentialField): string {
  if (providerForm.id && Object.hasOwn(providerForm.credentials, credential.key)) {
    return '已加密配置，留空保持不变'
  }
  return credential.placeholder
}

function credentialHelp(credential: ProviderCredentialField): string {
  return credential.help_text || `适配变量：{{credentials.${credential.key}}}`
}

function videoCapabilitySummary(model: AIModel): string[] {
  if (model.model_type !== 'video') return []
  try {
    const capabilities = normalizedVideoCapabilities(model.capabilities)
    const labels: Record<string, string> = {
      text_to_video: '文生视频',
      first_frame: '首帧',
      first_last_frame: '首尾帧',
      last_frame: '尾帧',
      full_reference: '全参考',
      multi_shot: '多镜头',
    }
    return capabilities.generation_modes.map((item) => labels[item] || item)
  } catch {
    return []
  }
}

function openAgent(agent?: AgentProfile): void {
  agentConfigBase.value = { ...(agent?.config ?? {}) }
  Object.assign(agentForm, {
    id: agent?.id ?? '',
    kind: agent?.kind ?? 'screenplay',
    name: agent?.name ?? '',
    description: agent?.description ?? '',
    system_prompt: agent?.system_prompt ?? '',
    text_model_id: agent?.text_model_id ?? models.value.find((item) => item.model_type === 'text')?.id ?? '',
    routing_priority: Number(agent?.config.routing_priority ?? 0),
    memory_enabled: agent?.memory_enabled ?? true,
    enabled: agent?.enabled ?? true,
  })
  dialog.value = 'agent'
}

function openPricing(rule: PricingRule): void {
  Object.assign(pricingForm, {
    task_type: rule.task_type,
    name: rule.name,
    description: rule.description,
    unit_label: rule.unit_label,
    unit_cost: formatCredits(rule.unit_cost),
    version: rule.version,
  })
  dialog.value = 'pricing'
}

function openPrompt(prompt: PromptTemplate): void {
  Object.assign(promptForm, {
    id: prompt.id,
    code: prompt.code,
    name: prompt.name,
    description: prompt.description,
    content: prompt.content,
    enabled: prompt.enabled,
  })
  dialog.value = 'prompt'
}

function clearPendingHandbookCover(): void {
  if (pendingHandbookCoverPreview.value) URL.revokeObjectURL(pendingHandbookCoverPreview.value)
  pendingHandbookCover.value = null
  pendingHandbookCoverPreview.value = null
  if (handbookCoverInput.value) handbookCoverInput.value.value = ''
}

function initializeHandbookFiles(handbookType: HandbookType): void {
  const manifest = handbookManifests.value.find((item) => item.handbook_type === handbookType)
  handbookFiles.value = (manifest?.files ?? []).map((item) => ({
    ...item,
    content: item.key === 'readme'
      ? `# ${handbookForm.name || (handbookType === 'visual' ? '新视觉手册' : '新导演手册')}\n\n请填写手册说明、适用范围和全局规则。`
      : `# ${item.label}\n\n## 目标\n\n${item.purpose}。\n\n## 约束\n\n- 请在此填写可执行且无歧义的约束。`,
  }))
  activeHandbookFileKey.value = handbookFiles.value[0]?.key ?? ''
}

function updateActiveHandbookFile(content: string): void {
  const file = activeHandbookFile.value
  if (file) file.content = content
}

async function openHandbook(handbook?: Handbook, handbookType: HandbookType = 'visual'): Promise<void> {
  clearPendingHandbookCover()
  Object.assign(handbookForm, {
    id: handbook?.id ?? '',
    handbook_type: handbook?.handbook_type ?? handbookType,
    name: handbook?.name ?? '',
    description: handbook?.description ?? '',
    cover_url: handbook?.cover_url ?? '',
    enabled: handbook?.enabled ?? true,
  })
  handbookFiles.value = []
  activeHandbookFileKey.value = ''
  dialog.value = 'handbook'
  if (!handbook) {
    initializeHandbookFiles(handbookType)
    return
  }
  loadingHandbookPackage.value = true
  try {
    const packageData = await api<HandbookPackage>(`/admin/handbooks/${handbook.id}/package`)
    handbookFiles.value = packageData.files
    activeHandbookFileKey.value = packageData.files[0]?.key ?? ''
  } catch (error) {
    closeDialog()
    toast.show('手册文件加载失败', {
      message: error instanceof Error ? error.message : undefined,
      tone: 'error',
    })
  } finally {
    loadingHandbookPackage.value = false
  }
}

function closeDialog(): void {
  if (dialog.value === 'handbook') clearPendingHandbookCover()
  if (dialog.value === 'handbook') {
    handbookFiles.value = []
    activeHandbookFileKey.value = ''
  }
  dialog.value = null
}

function chooseHandbookCover(event: Event): void {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) {
    toast.show('请选择 JPG、PNG 或 WebP 图片', { tone: 'error' })
    input.value = ''
    return
  }
  if (file.size > 8 * 1024 * 1024) {
    toast.show('封面图片不能超过 8 MB', { tone: 'error' })
    input.value = ''
    return
  }
  clearPendingHandbookCover()
  pendingHandbookCover.value = file
  pendingHandbookCoverPreview.value = URL.createObjectURL(file)
}

async function saveDialog(): Promise<void> {
  saving.value = true
  let handbookMetadataSaved = false
  try {
    if (dialog.value === 'provider') {
      if (providerAdapterState.value.error || !providerAdapterState.value.config) {
        throw new Error(providerAdapterState.value.error || '适配协议不可用')
      }
      const credentials = Object.fromEntries(
        Object.entries(providerForm.credentials).filter(([, value]) => value.trim()),
      )
      const payload = providerForm.id
        ? {
            name: providerForm.name,
            provider_type: providerForm.provider_type,
            base_url: providerForm.base_url,
            max_concurrency: providerForm.max_concurrency,
            enabled: providerForm.enabled,
            clear_api_key: providerForm.clear_api_key,
            adapter_config: providerAdapterState.value.config,
            ...(providerForm.api_key ? { api_key: providerForm.api_key } : {}),
            ...(Object.keys(credentials).length ? { credentials } : {}),
          }
        : {
            code: providerForm.code,
            name: providerForm.name,
            provider_type: providerForm.provider_type,
            base_url: providerForm.base_url,
            api_key: providerForm.api_key || null,
            credentials,
            adapter_config: providerAdapterState.value.config,
            max_concurrency: providerForm.max_concurrency,
            enabled: providerForm.enabled,
          }
      const path = providerForm.id ? `/admin/providers/${providerForm.id}` : '/admin/providers'
      await api(path, { method: providerForm.id ? 'PATCH' : 'POST', body: JSON.stringify(payload) })
    } else if (dialog.value === 'model') {
      const payload = modelForm.id
        ? {
            name: modelForm.name,
            ...(modelForm.model_type === 'video' ? { capabilities: modelForm.capabilities } : {}),
            enabled: modelForm.enabled,
            is_default: modelForm.is_default,
          }
        : {
            provider_id: modelForm.provider_id,
            model_id: modelForm.model_id,
            name: modelForm.name,
            model_type: modelForm.model_type,
            ...(modelForm.model_type === 'video' ? { capabilities: modelForm.capabilities } : {}),
            enabled: modelForm.enabled,
            is_default: modelForm.is_default,
          }
      const path = modelForm.id ? `/admin/models/${modelForm.id}` : '/admin/models'
      await api(path, { method: modelForm.id ? 'PATCH' : 'POST', body: JSON.stringify(payload) })
    } else if (dialog.value === 'pricing') {
      await api(`/admin/pricing-rules/${encodeURIComponent(pricingForm.task_type)}`, {
        method: 'PATCH',
        body: JSON.stringify({ unit_cost: pricingForm.unit_cost }),
      })
    } else if (dialog.value === 'agent') {
      const { id: _id, routing_priority, ...values } = agentForm
      const payload = {
        ...values,
        text_model_id: agentForm.text_model_id || null,
        config: { ...agentConfigBase.value, routing_priority },
      }
      const path = agentForm.id ? `/admin/agents/${agentForm.id}` : '/admin/agents'
      await api(path, { method: agentForm.id ? 'PUT' : 'POST', body: JSON.stringify(payload) })
    } else if (dialog.value === 'prompt') {
      await api(`/admin/prompts/${promptForm.id}`, {
        method: 'PUT',
        body: JSON.stringify({ content: promptForm.content }),
      })
    } else if (dialog.value === 'handbook') {
      if (!handbookFilesComplete.value) throw new Error('所有固定 Skills 文件都必须填写内容')
      const files = Object.fromEntries(handbookFiles.value.map((item) => [item.filename, item.content]))
      let handbook: Handbook
      if (handbookForm.id) {
        const packageData = await api<HandbookPackage>(`/admin/handbooks/${handbookForm.id}/package`, {
          method: 'PUT',
          body: JSON.stringify({
            name: handbookForm.name,
            description: handbookForm.description,
            enabled: handbookForm.enabled,
            files,
          }),
        })
        handbook = packageData.handbook
      } else {
        handbook = await api<Handbook>('/admin/handbooks', {
          method: 'POST',
          body: JSON.stringify({
            handbook_type: handbookForm.handbook_type,
            name: handbookForm.name,
            description: handbookForm.description,
            cover_url: handbookForm.cover_url || null,
            enabled: handbookForm.enabled,
            files,
          }),
        })
      }
      handbookForm.id = handbook.id
      handbookForm.cover_url = handbook.cover_url ?? ''
      handbookMetadataSaved = true
      if (pendingHandbookCover.value) {
        const body = new FormData()
        body.append('file', pendingHandbookCover.value)
        await api<Handbook>(`/admin/handbooks/${handbook.id}/cover/upload`, { method: 'POST', body })
      }
    }
    closeDialog()
    toast.show('配置已保存', { tone: 'success' })
    await loadAll()
  } catch (error) {
    toast.show(handbookMetadataSaved ? '手册已保存，但封面上传失败' : '保存失败', {
      message: error instanceof Error ? error.message : undefined,
      tone: 'error',
    })
    if (handbookMetadataSaved) await loadAll()
  } finally {
    saving.value = false
  }
}

onBeforeUnmount(clearPendingHandbookCover)

async function testProvider(provider: Provider): Promise<void> {
  testingProviderId.value = provider.id
  try {
    const result = await api<{ ok: boolean; message: string; latency_ms: number }>(`/admin/providers/${provider.id}/test`, {
      method: 'POST',
    })
    toast.show(result.ok ? '连接测试成功' : '连接测试失败', {
      message: `${result.message} · ${result.latency_ms}ms`,
      tone: result.ok ? 'success' : 'error',
    })
    await loadAll()
  } catch (error) {
    toast.show('连接测试失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    testingProviderId.value = null
  }
}

async function testModel(model: AIModel): Promise<void> {
  if (testingModelId.value) return
  testingModelId.value = model.id
  try {
    const result = await api<{ ok: boolean; message: string; latency_ms: number }>(
      `/admin/models/${model.id}/test`,
      { method: 'POST' },
    )
    toast.show(result.ok ? `${model.name} 验证通过` : `${model.name} 验证异常`, {
      message: `${result.message} · ${result.latency_ms}ms`,
      tone: result.ok ? 'success' : 'error',
    })
    await loadAll()
  } catch (error) {
    toast.show('模型验证失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    testingModelId.value = null
  }
}

function modelTestLabel(model: AIModel): string {
  if (model.last_test_ok === true) return model.model_type === 'image' ? '实测通过' : '验证通过'
  if (model.last_test_ok === false) return '验证异常'
  return '尚未验证'
}

async function discoverModels(provider: Provider): Promise<void> {
  discoveryProvider.value = provider
  discoveryRows.value = []
  discoverySearch.value = ''
  discovering.value = true
  try {
    const result = await api<{ provider_id: string; items: DiscoveredModel[] }>(`/admin/providers/${provider.id}/discover-models`, { method: 'POST' })
    discoveryRows.value = result.items.map((item) => ({
      ...item,
      selected: !item.is_imported,
      model_type: item.inferred_type,
    }))
  } catch (error) {
    discoveryProvider.value = null
    toast.show('模型目录获取失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    discovering.value = false
  }
}

function toggleAllDiscovered(): void {
  const next = !allDiscoverySelected.value
  selectableDiscoveryRows.value.forEach((item) => (item.selected = next))
}

async function importDiscoveredModels(): Promise<void> {
  if (!discoveryProvider.value || selectedDiscoveryCount.value === 0) return
  importing.value = true
  try {
    const items = discoveryRows.value
      .filter((item) => item.selected && !item.is_imported)
      .map((item) => ({ model_id: item.model_id, name: item.name, model_type: item.model_type, enabled: true }))
    const result = await api<{ imported: AIModel[]; skipped_model_ids: string[] }>(
      `/admin/providers/${discoveryProvider.value.id}/import-models`,
      { method: 'POST', body: JSON.stringify({ items }) },
    )
    toast.show(`已导入 ${result.imported.length} 个模型`, {
      message: result.skipped_model_ids.length ? `${result.skipped_model_ids.length} 个重复模型已跳过` : undefined,
      tone: 'success',
    })
    discoveryProvider.value = null
    await loadAll()
  } catch (error) {
    toast.show('模型导入失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    importing.value = false
  }
}

async function confirmDelete(): Promise<void> {
  if (!deleteTarget.value) return
  deleting.value = true
  try {
    await api(`/admin/${deleteTarget.value.kind === 'provider' ? 'providers' : 'models'}/${deleteTarget.value.id}`, { method: 'DELETE' })
    toast.show(`${deleteTarget.value.name} 已删除`, { tone: 'success' })
    deleteTarget.value = null
    await loadAll()
  } catch (error) {
    toast.show('无法删除', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    deleting.value = false
  }
}

async function selectSkill(node: SkillNode): Promise<void> {
  if (skillDirty.value && !window.confirm('当前文件有未保存的修改，确定切换吗？')) return
  const file = await api<{ path: string; content: string }>(`/admin/skills/file?path=${encodeURIComponent(node.path)}`)
  selectedSkill.value = file.path
  skillContent.value = file.content
  skillDirty.value = false
}

async function saveSkill(): Promise<void> {
  if (!selectedSkill.value) return
  saving.value = true
  try {
    const saved = await api<{ path: string; content: string }>(`/admin/skills/file?path=${encodeURIComponent(selectedSkill.value)}`, {
      method: 'PUT',
      body: JSON.stringify({ content: skillContent.value }),
    })
    skillContent.value = saved.content
    skillDirty.value = false
    const [promptRows, handbookRows, skillRows] = await Promise.all([
      api<PromptTemplate[]>('/admin/prompts'),
      api<Handbook[]>('/admin/handbooks'),
      api<SkillNode[]>('/admin/skills/tree'),
    ])
    prompts.value = promptRows
    handbooks.value = handbookRows
    skillTree.value = skillRows
    toast.show('Skills 文件已保存', { tone: 'success' })
  } catch (error) {
    toast.show('文件保存失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="admin-page page-stack">
    <header class="page-header admin-page__header">
      <div>
        <span class="eyebrow">ADMIN CONSOLE</span>
        <h1>管理控制台</h1>
        <p>模型、Agent 与创作规则</p>
      </div>
      <span v-if="readiness" class="readiness-badge" :data-ready="readiness.ready">
        <Check v-if="readiness.ready" :size="16" />
        <CircleAlert v-else :size="16" />
        {{ readiness.ready ? '核心模型已就绪' : '存在必填配置' }}
      </span>
    </header>

    <nav ref="adminTabs" class="admin-tabs" aria-label="管理模块">
      <RouterLink v-for="item in tabs" :key="item.id" :to="`/admin/${item.id}`" :class="{ active: section === item.id }">
        <component :is="item.icon" :size="17" />
        <span>{{ item.label }}</span>
      </RouterLink>
    </nav>

    <div v-if="loading" class="page-loading"><span class="spinner"></span></div>

    <template v-else>
      <section v-if="section === 'overview'" class="admin-overview">
        <div class="metric-grid">
          <article class="metric"><span>平台连接</span><strong class="tabular-nums">{{ providers.length }}</strong><Network :size="20" /></article>
          <article class="metric"><span>可用模型</span><strong class="tabular-nums">{{ models.filter((item) => item.enabled).length }}</strong><Cpu :size="20" /></article>
          <article class="metric"><span>Agent 配置</span><strong class="tabular-nums">{{ agents.length }}</strong><Bot :size="20" /></article>
          <article class="metric"><span>创作手册</span><strong class="tabular-nums">{{ handbooks.length }}</strong><BookOpen :size="20" /></article>
        </div>
        <div class="readiness-panel">
          <div class="section-heading"><div><h2>系统就绪状态</h2><p>完整创作链路所需的默认模型</p></div></div>
          <div class="readiness-list">
            <div v-for="type in (['text', 'image', 'video'] as const)" :key="type">
              <span class="status-icon" :data-ok="readiness?.required_defaults[type]">
                <Check v-if="readiness?.required_defaults[type]" :size="15" />
                <CircleAlert v-else :size="15" />
              </span>
              <span>默认{{ modelTypeLabel[type] }}模型</span>
              <strong>{{ readiness?.required_defaults[type] ? '已配置' : '待配置' }}</strong>
            </div>
            <div>
              <span class="status-icon" :data-ok="readiness?.optional_defaults.tts"><Check :size="15" /></span>
              <span>默认 TTS 模型</span>
              <strong>{{ readiness?.optional_defaults.tts ? '已配置' : '可选' }}</strong>
            </div>
          </div>
        </div>
      </section>

      <AdminUsersPanel v-else-if="section === 'users'" />

      <section v-else-if="section === 'models'" class="admin-section">
        <header class="section-heading">
          <div><h2>模型服务</h2><p>供应商接入、万能适配协议与模型能力在同一工作台维护</p></div>
          <div class="section-actions">
            <button class="button button--secondary" type="button" :disabled="installingAutoDlPreset || autoDlPresetInstalled" @click="installAutoDlPreset"><LoaderCircle v-if="installingAutoDlPreset" class="spin" :size="17" /><Film v-else :size="17" />{{ autoDlPresetInstalled ? 'AutoDL H3 已添加' : '添加 AutoDL H3' }}</button>
            <button class="button button--secondary" type="button" @click="openProvider()"><Plus :size="17" />接入供应商</button>
            <button class="button button--primary" type="button" :disabled="!activeProvider" @click="openModel(undefined, activeProvider?.id)"><Plus :size="17" />添加模型</button>
          </div>
        </header>
        <section class="global-defaults" aria-labelledby="global-defaults-title">
          <header class="global-defaults__header">
            <div>
              <span><ShieldCheck :size="19" /></span>
              <div><h3 id="global-defaults-title">全局默认模型</h3><p>首页 Agent 与未指定模型的创作任务自动使用；配置仅对管理员开放</p></div>
            </div>
            <span class="global-defaults__readiness" :data-ready="readiness?.ready">
              <Check v-if="readiness?.ready" :size="15" />
              <CircleAlert v-else :size="15" />
              {{ readiness?.ready ? '核心能力已就绪' : '核心能力未就绪' }}
            </span>
          </header>
          <div class="global-defaults__grid">
            <article
              v-for="(type, index) in (['text', 'image', 'video', 'tts'] as ModelType[])"
              :key="type"
              v-motion="{ preset: 'card', index }"
              class="default-model-slot"
              :data-configured="Boolean(defaultModelFor(type))"
            >
              <header>
                <span><component :is="modelTypeIcon[type]" :size="19" /></span>
                <div><strong>默认{{ modelTypeLabel[type] }}模型</strong><small>{{ type === 'tts' ? '可选能力' : '必填能力' }}</small></div>
                <i><Check v-if="defaultModelFor(type)" :size="13" /><CircleAlert v-else :size="13" />{{ defaultModelFor(type) ? '已配置' : '未配置' }}</i>
              </header>
              <div v-if="defaultModelFor(type)" class="default-model-slot__current">
                <strong>{{ defaultModelFor(type)?.name }}</strong>
                <small>{{ providerForModel(defaultModelFor(type))?.name }} · {{ defaultModelFor(type)?.model_id }}</small>
              </div>
              <div v-else class="default-model-slot__current default-model-slot__current--empty">
                <strong>尚未选择</strong>
                <small>{{ type === 'tts' ? '不影响图片与视频功能' : '相关创作能力暂不可用' }}</small>
              </div>
              <UiSelect
                v-if="defaultModelOptions(type).length"
                :model-value="defaultModelFor(type)?.id ?? ''"
                :options="defaultModelOptions(type)"
                :placeholder="`选择默认${modelTypeLabel[type]}模型`"
                :disabled="Boolean(settingDefaultType)"
                variant="compact"
                @update:model-value="setDefaultModel(type, $event)"
              />
              <button
                v-else
                class="default-model-slot__add"
                type="button"
                :disabled="!activeProvider"
                @click="openModel(undefined, activeProvider?.id, type)"
              ><Plus :size="16" /><span>添加可用{{ modelTypeLabel[type] }}模型</span></button>
              <span v-if="settingDefaultType === type" class="default-model-slot__saving"><LoaderCircle class="spin" :size="14" />正在切换</span>
            </article>
          </div>
        </section>
        <div class="model-service-workbench">
          <aside class="provider-directory">
            <header><div><span>供应商</span><strong class="tabular-nums">{{ providers.length }}</strong></div><button class="icon-button icon-button--small" type="button" title="接入供应商" @click="openProvider()"><Plus :size="16" /></button></header>
            <div class="provider-directory__list">
              <button v-for="(provider, index) in providers" :key="provider.id" v-motion="{ preset: 'row', index }" type="button" :class="{ active: activeProvider?.id === provider.id }" @click="activeProviderId = provider.id">
                <span class="provider-directory__icon"><ServerCog :size="18" /></span>
                <div><strong>{{ provider.name }}</strong><small>{{ providerModelCount(provider.id) }} 个模型</small></div>
                <i :data-active="provider.enabled"></i>
              </button>
            </div>
            <button class="provider-directory__add" type="button" @click="openProvider()"><Plus :size="17" /><span><strong>添加供应商</strong><small>标准平台或万能适配器</small></span></button>
          </aside>

          <section v-if="activeProvider" class="provider-console">
            <header class="provider-console__header">
              <div class="provider-console__identity">
                <span><Network :size="22" /></span>
                <div><div><h3>{{ activeProvider.name }}</h3><i class="status-dot" :data-active="activeProvider.enabled">{{ activeProvider.enabled ? '运行中' : '已停用' }}</i></div><p>{{ activeProvider.base_url }}</p></div>
              </div>
              <div class="provider-console__actions">
                <button class="button button--secondary" type="button" :disabled="testingProviderId === activeProvider.id" @click="testProvider(activeProvider)"><LoaderCircle v-if="testingProviderId === activeProvider.id" class="spin" :size="16" /><TestTube2 v-else :size="16" />测试连接</button>
                <button class="button button--secondary" type="button" @click="discoverModels(activeProvider)"><Download :size="16" />获取模型</button>
                <button class="icon-button" type="button" title="编辑供应商" @click="openProvider(activeProvider)"><Settings2 :size="17" /></button>
                <button class="icon-button icon-button--danger" type="button" title="删除供应商" @click="deleteTarget = { kind: 'provider', id: activeProvider.id, name: activeProvider.name }"><Trash2 :size="17" /></button>
              </div>
            </header>
            <div class="provider-console__meta">
              <span><ServerCog :size="14" />{{ providerTypeOptions.find((item) => item.value === activeProvider?.provider_type)?.label }}</span>
              <span><KeyRound :size="14" />{{ activeProvider.has_api_key || activeProviderCredentialCount ? `${activeProviderCredentialCount || 1} 项凭据已加密` : '未配置凭据' }}</span>
              <span><Gauge :size="14" />最多 <strong class="tabular-nums">{{ activeProvider.max_concurrency }}</strong> 个工作流</span>
              <span v-if="activeProviderHasAdapter"><Code2 :size="14" />万能适配协议</span>
              <span :data-ok="activeProvider.last_test_ok"><Check v-if="activeProvider.last_test_ok" :size="14" /><CircleAlert v-else :size="14" />{{ activeProvider.last_test_message || '尚未测试连接' }}</span>
            </div>

            <div class="provider-models-heading"><div><span>模型清单</span><strong class="tabular-nums">{{ activeProviderModels.length }}</strong></div><button class="button button--primary" type="button" @click="openModel(undefined, activeProvider.id)"><Plus :size="16" />手动添加</button></div>
            <div v-if="activeProviderModels.length" class="provider-model-grid" aria-label="当前供应商模型">
              <article v-for="(model, index) in activeProviderModels" :key="model.id" v-motion="{ preset: 'card', index }" class="provider-model-card" :data-health="model.last_test_ok === null ? 'unknown' : model.last_test_ok ? 'ok' : 'failed'">
                <header><span><component :is="modelTypeIcon[model.model_type]" :size="20" /></span><div><strong>{{ model.name }}</strong><code>{{ model.model_id }}</code></div><i v-if="model.is_default"><Check :size="13" />默认</i></header>
                <div class="provider-model-card__badges">
                  <span><component :is="modelTypeIcon[model.model_type]" :size="13" />{{ modelTypeLabel[model.model_type] }}</span>
                  <span v-for="capability in videoCapabilitySummaries[model.id]?.slice(0, 3) ?? []" :key="capability">{{ capability }}</span>
                  <span v-if="(videoCapabilitySummaries[model.id]?.length ?? 0) > 3">+{{ (videoCapabilitySummaries[model.id]?.length ?? 0) - 3 }}</span>
                </div>
                <div class="provider-model-card__health"><span><i></i>{{ modelTestLabel(model) }}</span><small v-if="model.last_test_latency_ms !== null" class="tabular-nums">{{ model.last_test_latency_ms }} ms</small><small v-else>{{ model.enabled ? '模型已启用' : '模型已停用' }}</small></div>
                <footer>
                  <button class="button button--secondary" type="button" :disabled="Boolean(testingModelId)" :title="model.model_type === 'image' ? '将真实生成一张测试图，可能产生上游费用' : '验证模型配置与连接'" @click="testModel(model)"><LoaderCircle v-if="testingModelId === model.id" class="spin" :size="15" /><TestTube2 v-else :size="15" />{{ model.model_type === 'image' ? '实测生成' : '验证' }}</button>
                  <button class="button button--ghost" type="button" @click="openModel(model)"><Pencil :size="15" />编辑</button>
                  <button class="icon-button icon-button--small icon-button--danger" type="button" :disabled="model.is_default" :title="model.is_default ? '请先切换同类型默认模型' : '删除模型'" @click="deleteTarget = { kind: 'model', id: model.id, name: model.name }"><Trash2 :size="15" /></button>
                </footer>
              </article>
            </div>
            <div v-else class="provider-model-empty"><span><Layers3 :size="26" /></span><strong>这个供应商还没有模型</strong><p>自动读取模型目录，或手动添加并配置完整的视频能力。</p><div><button class="button button--secondary" type="button" @click="discoverModels(activeProvider)"><Download :size="16" />获取模型</button><button class="button button--primary" type="button" @click="openModel(undefined, activeProvider.id)"><Plus :size="16" />手动添加</button></div></div>
          </section>
          <div v-else class="provider-console provider-console--empty"><span><ServerCog :size="30" /></span><strong>接入第一个模型供应商</strong><p>可使用 OpenAI 兼容目录，也可配置自定义请求、轮询和结果映射。</p><button class="button button--primary" type="button" @click="openProvider()"><Plus :size="17" />接入供应商</button></div>
        </div>
      </section>

      <section v-else-if="section === 'pricing'" class="admin-section pricing-section">
        <header class="section-heading"><div><h2>计费规则</h2><p>配置当前租户后续 AI 生产任务的积分单价</p></div><span class="pricing-count"><Coins :size="15" /><strong class="tabular-nums">{{ pricingRules.length }}</strong> 项规则</span></header>
        <aside class="pricing-policy">
          <span><ShieldCheck :size="19" /></span>
          <div><strong>任务创建时冻结价格</strong><p>单价调整仅影响之后提交的任务；已排队、执行中、失败重试与退款任务继续使用原始价格快照。</p></div>
        </aside>
        <div class="pricing-grid">
          <button v-for="(rule, index) in pricingRules" :key="rule.id" v-motion="{ preset: 'card', index }" class="pricing-rule" type="button" @click="openPricing(rule)">
            <span class="pricing-rule__icon"><Coins :size="19" /></span>
            <div class="pricing-rule__identity"><strong>{{ rule.name }}</strong><p>{{ rule.description }}</p><code>{{ rule.task_type }} · v{{ rule.version }}</code></div>
            <div class="pricing-rule__amount"><strong class="tabular-nums">{{ formatCredits(rule.unit_cost) }}</strong><span>积分 / {{ rule.unit_label }}</span></div>
          </button>
        </div>
      </section>

      <section v-else-if="section === 'agents'" class="admin-section">
        <header class="section-heading"><div><h2>Agent 配置</h2><p>剧本改编与通用 AI 能力</p></div><button class="button button--primary" type="button" @click="openAgent()"><Plus :size="17" />新建 Agent</button></header>
        <div class="settings-list">
          <button v-for="(agent, index) in agents" :key="agent.id" v-motion="{ preset: 'row', index }" class="settings-row" type="button" @click="openAgent(agent)">
            <span class="settings-row__icon"><Bot :size="19" /></span>
            <div><strong>{{ agent.name }}</strong><p>{{ agent.description }}</p></div>
            <span class="type-chip">{{ agent.kind === 'screenplay' ? '剧本 Agent' : '通用 AI' }}</span>
            <span class="publication-state" :data-enabled="agent.enabled"><Check v-if="agent.enabled" :size="13" /><CircleAlert v-else :size="13" />{{ agent.enabled ? '已发布' : '已停用' }}</span>
            <span class="settings-row__version">v{{ agent.version }}</span>
          </button>
        </div>
      </section>

      <section v-else-if="section === 'prompts'" class="admin-section">
        <header class="section-heading"><div><h2>提示词管理</h2><p>系统底层功能提示词，仅允许编辑正文</p></div><span class="managed-count"><LockKeyhole :size="15" />{{ prompts.length }} 项固定配置</span></header>
        <div class="prompt-grid">
          <button v-for="(prompt, index) in prompts" :key="prompt.id" v-motion="{ preset: 'card', index }" class="prompt-item" type="button" @click="openPrompt(prompt)">
            <span class="prompt-item__icon"><FileCheck2 :size="20" /></span>
            <div class="prompt-item__copy"><strong>{{ prompt.name }}</strong><code>{{ prompt.code }}</code><p>{{ prompt.description }}</p></div>
            <span class="prompt-item__version">v{{ prompt.version }}</span>
            <Pencil class="prompt-item__edit" :size="16" />
          </button>
        </div>
      </section>

      <section v-else-if="section === 'handbooks'" class="admin-section">
        <header class="section-heading handbook-heading"><div><h2>创作手册</h2><p>固定结构的视觉与导演 Skills 技能包</p></div><div><button class="button button--secondary" type="button" @click="openHandbook(undefined, 'director')"><BookMarked :size="17" />新建导演手册</button><button class="button button--primary" type="button" @click="openHandbook(undefined, 'visual')"><Sparkles :size="17" />新建视觉手册</button></div></header>
        <div class="handbook-grid">
          <button v-for="(handbook, index) in handbooks" :key="handbook.id" v-motion="{ preset: 'card', index }" class="handbook-item" type="button" @click="openHandbook(handbook)">
            <div class="handbook-item__cover"><img :src="handbook.cover_url || '/covers/login-studio.jpg'" :alt="handbook.name" /></div>
            <div>
              <div class="handbook-item__meta"><span class="type-chip">{{ handbook.handbook_type === 'visual' ? '视觉手册' : '导演手册' }}</span><span class="publication-state" :data-enabled="handbook.enabled"><Check v-if="handbook.enabled" :size="13" /><CircleAlert v-else :size="13" />{{ handbook.enabled ? '已发布' : '已停用' }}</span></div>
              <h3>{{ handbook.name }}</h3><p>{{ handbook.description }}</p><small>v{{ handbook.version }} · {{ handbook.handbook_type === 'visual' ? '12 个固定文件' : '3 个固定文件' }}</small>
            </div>
          </button>
        </div>
      </section>

      <section v-else-if="section === 'skills'" class="admin-section skills-manager">
        <header class="section-heading"><div><h2>Skills 文件</h2><p>视觉手册、导演手册与系统提示词的统一文件视图</p></div><button class="button button--primary" type="button" :disabled="!skillDirty || saving" @click="saveSkill"><Save :size="17" />保存</button></header>
        <div class="skills-workbench">
          <aside><SkillTree :nodes="skillTree" :selected="selectedSkill" @select="selectSkill" /></aside>
          <section class="skill-editor">
            <header><FileCode2 :size="17" /><span>{{ selectedSkill || '选择文件' }}</span><i v-if="skillDirty"></i></header>
            <textarea v-if="selectedSkill" v-model="skillContent" spellcheck="false" @input="skillDirty = true"></textarea>
            <div v-else class="editor-empty"><FileCode2 :size="28" /><span>从左侧选择一个 Skills 文件</span></div>
          </section>
        </div>
      </section>

      <section v-else-if="section === 'security'" class="admin-section security-audit">
        <header class="section-heading">
          <div><h2>安全审计</h2><p>租户登录、锁定与访问限流记录</p></div>
          <button class="icon-button security-refresh" type="button" title="刷新安全事件" :disabled="securityLoading" @click="loadSecurityEvents(true)">
            <RefreshCw :class="{ spin: securityLoading }" :size="17" />
          </button>
        </header>

        <div class="security-summary" aria-label="当前安全事件摘要">
          <article>
            <span class="security-summary__icon"><ShieldCheck :size="19" /></span>
            <div><span>当前记录</span><strong class="tabular-nums">{{ securityEvents.length }}</strong></div>
          </article>
          <article data-tone="success">
            <span class="security-summary__icon"><LogIn :size="19" /></span>
            <div><span>验证通过</span><strong class="tabular-nums">{{ securitySuccessCount }}</strong></div>
          </article>
          <article data-tone="danger">
            <span class="security-summary__icon"><LockKeyhole :size="19" /></span>
            <div><span>风险事件</span><strong class="tabular-nums">{{ securityFailureCount }}</strong></div>
          </article>
        </div>

        <div class="security-toolbar">
          <UiSelect v-model="securityType" class="security-type-select" :options="securityTypeOptions" />
          <div class="security-outcome" role="group" aria-label="按验证结果筛选">
            <button v-for="item in ([{ value: 'all', label: '全部' }, { value: 'success', label: '通过' }, { value: 'failed', label: '风险' }] as const)" :key="item.value" type="button" :class="{ active: securityOutcome === item.value }" :aria-pressed="securityOutcome === item.value" @click="securityOutcome = item.value">
              {{ item.label }}
            </button>
          </div>
        </div>

        <div v-if="securityLoading" class="security-loading"><LoaderCircle class="spin" :size="24" /><span>正在读取审计记录</span></div>
        <div v-else-if="securityEvents.length" class="security-event-list">
          <article v-for="(event, index) in securityEvents" :key="event.id" v-motion="{ preset: 'row', index }" class="security-event" :data-tone="securityMeta(event).tone">
            <span class="security-event__icon"><component :is="securityMeta(event).icon" :size="18" /></span>
            <div class="security-event__identity">
              <strong>{{ securityMeta(event).label }}</strong>
              <span>{{ securityDetail(event) }}</span>
            </div>
            <div class="security-event__fingerprint" title="账号身份指纹">
              <Fingerprint :size="14" />
              <code>{{ shortHash(event.subject_hash) }}</code>
            </div>
            <div class="security-event__fingerprint" title="来源网络指纹">
              <Network :size="14" />
              <code>{{ shortHash(event.ip_hash) }}</code>
            </div>
            <span class="security-event__client" :title="event.user_agent">{{ event.user_agent || '未知客户端' }}</span>
            <time class="tabular-nums" :datetime="event.created_at">{{ formatSecurityTime(event.created_at) }}</time>
          </article>
          <button v-if="securityNextBefore && securityNextBeforeId" class="button button--secondary security-load-more" type="button" :disabled="securityLoadingMore" @click="loadSecurityEvents(false)">
            <LoaderCircle v-if="securityLoadingMore" class="spin" :size="16" />
            <Download v-else :size="16" />
            加载更早记录
          </button>
        </div>
        <div v-else class="security-empty">
          <ShieldCheck :size="28" />
          <strong>当前筛选下没有安全事件</strong>
          <span>新的登录与风控记录会自动出现在这里</span>
        </div>
      </section>
    </template>

    <BaseDialog
      :open="dialog !== null"
      :title="dialogTitle"
      wide
      :workbench="dialog === 'handbook' || dialog === 'provider' || (dialog === 'model' && modelForm.model_type === 'video')"
      @update:open="!$event && closeDialog()"
    >
      <form id="admin-form" class="admin-form" @submit.prevent="saveDialog">
        <template v-if="dialog === 'provider'">
          <div class="provider-editor-shell field--full">
            <section class="provider-editor-section">
              <header><span><ServerCog :size="19" /></span><div><strong>供应商身份</strong><p>基础地址、平台类型与兼容协议</p></div></header>
              <div class="provider-editor-grid">
                <label class="field"><span>平台标识</span><input v-model="providerForm.code" :disabled="Boolean(providerForm.id)" required placeholder="my-gateway" /><small>创建后不可修改，用于日志与追踪</small></label>
                <label class="field"><span>显示名称</span><input v-model="providerForm.name" required placeholder="例如 AutoDL MiniMax H3" /></label>
                <label class="field"><span>平台类型</span><UiSelect v-model="providerForm.provider_type" :options="providerTypeOptions" /></label>
                <label class="field"><span>Base URL</span><input v-model="providerForm.base_url" type="url" required placeholder="https://api.example.com/v1" /></label>
                <div class="provider-concurrency-control field--full">
                  <span><Gauge :size="20" /></span>
                  <div><strong>最大并发工作流</strong><small>达到上限后，新任务保持排队，不占用其他供应商容量</small></div>
                  <div class="provider-concurrency-stepper" role="group" aria-label="供应商最大并发工作流">
                    <button type="button" title="减少并发数" :disabled="providerForm.max_concurrency <= 1" @click="providerForm.max_concurrency = Math.max(1, providerForm.max_concurrency - 1)"><Minus :size="16" /></button>
                    <strong class="tabular-nums" aria-live="polite">{{ providerForm.max_concurrency }}</strong>
                    <button type="button" title="增加并发数" :disabled="providerForm.max_concurrency >= 64" @click="providerForm.max_concurrency = Math.min(64, providerForm.max_concurrency + 1)"><Plus :size="16" /></button>
                  </div>
                </div>
              </div>
            </section>

            <section class="provider-editor-section">
              <header><span><KeyRound :size="19" /></span><div><strong>访问凭据</strong><p>所有敏感值经服务端加密，只显示配置状态</p></div></header>
              <div class="provider-editor-grid">
                <label class="field field--full"><span>OpenAI 兼容 API Key</span><input v-model="providerForm.api_key" type="password" autocomplete="new-password" :placeholder="providerForm.id ? '已配置则留空保持不变' : 'sk-...'" /><small>标准 Bearer 鉴权使用；万能适配器可改用下方自定义字段</small></label>
                <label v-for="credential in providerCredentialFields" :key="credential.key" class="field"><span>{{ credential.label }} <i v-if="credential.required">必填</i></span><input v-model="providerForm.credentials[credential.key]" :type="credential.input_type" autocomplete="new-password" :required="!providerForm.id && credential.required" :placeholder="credentialPlaceholder(credential)" /><small>{{ credentialHelp(credential) }}</small></label>
                <button v-if="providerForm.id" class="credential-clear-control" type="button" :class="{ active: providerForm.clear_api_key }" :aria-pressed="providerForm.clear_api_key" @click="providerForm.clear_api_key = !providerForm.clear_api_key"><span><Trash2 :size="16" /></span><div><strong>清除兼容密钥</strong><small>保存后删除现有 API Key</small></div><i></i></button>
              </div>
            </section>

            <section class="provider-editor-section adapter-editor-section">
              <header><span><Code2 :size="19" /></span><div><strong>万能自定义适配器</strong><p>配置请求、轮询、状态与结果提取，不执行任意服务器代码</p></div><button class="button button--secondary" type="button" @click="providerForm.adapter_expanded = !providerForm.adapter_expanded"><Code2 :size="15" />{{ providerForm.adapter_expanded ? '收起协议' : '编辑协议' }}</button></header>
              <div v-if="providerForm.adapter_expanded" class="adapter-editor">
                <div class="adapter-editor__toolbar"><span :data-error="Boolean(providerAdapterState.error)"><CircleAlert v-if="providerAdapterState.error" :size="15" /><Check v-else :size="15" />{{ providerAdapterState.error || '协议结构可解析，保存时服务端会再次严格校验' }}</span><button class="button button--ghost" type="button" @click="useCustomAdapterTemplate"><Sparkles :size="15" />载入视频模板</button></div>
                <textarea v-model="providerForm.adapter_json" spellcheck="false" aria-label="供应商适配协议 JSON"></textarea>
                <div class="adapter-variable-list"><span>可用变量</span><code v-for="variable in adapterVariables" :key="variable">{{ variable }}</code></div>
              </div>
              <button v-else class="adapter-editor-collapsed" type="button" @click="providerForm.adapter_expanded = true"><span><Code2 :size="20" /></span><div><strong>{{ providerAdapterState.config && Object.keys(providerAdapterState.config).length > 2 ? '已配置自定义适配协议' : '使用标准兼容协议' }}</strong><small>展开后可定义自有凭据、创建请求、轮询和结果路径</small></div><Pencil :size="16" /></button>
            </section>

            <section class="provider-editor-section provider-publish-section">
              <button class="capability-switch" type="button" :disabled="editingProviderHasEnabledModels" :aria-pressed="providerForm.enabled" @click="providerForm.enabled = !providerForm.enabled"><span><i></i></span><div><strong>启用此供应商</strong><small>停用后其下所有模型都不可用于新任务</small></div></button>
              <div v-if="editingProviderHasEnabledModels" class="config-protection-note"><LockKeyhole :size="16" /><span>平台下仍有启用模型。请先迁移或停用这些模型，再停用当前平台。</span></div>
            </section>
          </div>
        </template>
        <template v-else-if="dialog === 'model'">
          <div class="model-editor-identity field--full">
            <span><component :is="modelTypeIcon[modelForm.model_type]" :size="22" /></span>
            <div><strong>{{ modelForm.name || '新模型' }}</strong><p>{{ modelForm.model_id || '配置模型标识和真实能力' }}</p></div>
            <span v-if="modelForm.model_type === 'video'"><Film :size="14" />视频能力配置</span>
          </div>
          <label class="field"><span>所属平台</span><UiSelect v-model="modelForm.provider_id" :options="providerOptions" :disabled="Boolean(modelForm.id)" /></label>
          <label class="field"><span>模型类型</span><UiSelect v-model="modelForm.model_type" :options="modelTypeOptions" :disabled="Boolean(modelForm.id)" /></label>
          <label class="field"><span>模型 ID</span><input v-model="modelForm.model_id" :disabled="Boolean(modelForm.id)" required placeholder="上游请求使用的模型标识" /></label>
          <label class="field"><span>显示名称</span><input v-model="modelForm.name" required placeholder="便于管理员与项目识别" /></label>
          <VideoCapabilityEditor v-if="modelForm.model_type === 'video'" v-model="modelForm.capabilities" />
          <div class="model-publish-controls field--full">
            <button class="capability-switch" type="button" :disabled="editingRequiredDefault" :aria-pressed="modelForm.is_default" @click="modelForm.is_default = !modelForm.is_default"><span><i></i></span><div><strong>默认模型</strong><small>项目未指定模型时自动使用</small></div></button>
            <button class="capability-switch" type="button" :disabled="editingRequiredDefault" :aria-pressed="modelForm.enabled" @click="modelForm.enabled = !modelForm.enabled"><span><i></i></span><div><strong>启用模型</strong><small>允许新项目和生成任务选择</small></div></button>
          </div>
          <div v-if="editingRequiredDefault" class="config-protection-note field--full"><LockKeyhole :size="16" /><span>这是完整创作链路依赖的默认模型。请先将同类型的其他模型设为默认，再停用或删除当前模型。</span></div>
        </template>
        <template v-else-if="dialog === 'pricing'">
          <div class="pricing-edit-summary field--full"><span><Coins :size="20" /></span><div><strong>{{ pricingForm.name }}</strong><p>{{ pricingForm.description }}</p><code>{{ pricingForm.task_type }} · v{{ pricingForm.version }}</code></div></div>
          <label class="field field--full"><span>任务单价</span><div class="pricing-input-control"><Coins :size="17" /><input v-model="pricingForm.unit_cost" type="number" min="0" max="1000000" step="0.01" inputmode="decimal" required /><span>积分 / {{ pricingForm.unit_label }}</span></div></label>
          <div class="config-protection-note field--full"><ShieldCheck :size="16" /><span>保存后只对新创建任务生效。历史任务、重试和退款均保留创建时的价格快照。</span></div>
        </template>
        <template v-else-if="dialog === 'agent'">
          <label class="field"><span>Agent 类型</span><UiSelect v-model="agentForm.kind" :options="agentKindOptions" /></label>
          <label class="field"><span>名称</span><input v-model="agentForm.name" required /></label>
          <label class="field field--full"><span>说明</span><input v-model="agentForm.description" /></label>
          <label class="field"><span>文本模型</span><UiSelect v-model="agentForm.text_model_id" :options="textModelOptions" /></label>
          <label class="field"><span>自动调用优先级</span><input v-model.number="agentForm.routing_priority" type="number" min="0" max="1000" step="1" /><small>同类型 Agent 优先调用数值更高的配置</small></label>
          <label class="check-field"><input v-model="agentForm.memory_enabled" type="checkbox" /><span>启用持久记忆</span></label>
          <label class="check-field"><input v-model="agentForm.enabled" type="checkbox" /><span>发布此 Agent</span></label>
          <label class="field field--full"><span>系统提示词</span><textarea v-model="agentForm.system_prompt" rows="10" required></textarea></label>
        </template>
        <template v-else-if="dialog === 'prompt'">
          <div class="prompt-editor-identity field--full">
            <span><FileCheck2 :size="20" /></span>
            <div><strong>{{ promptForm.name }}</strong><p>{{ promptForm.description }}</p><code>{{ promptForm.code }} · 系统固定配置</code></div>
            <span class="managed-lock"><LockKeyhole :size="14" />不可新增或删除</span>
          </div>
          <label class="field field--full prompt-content-field"><span>提示词正文</span><textarea v-model="promptForm.content" rows="18" required spellcheck="false"></textarea><small>保存后立即供对应 AI 功能调用，并同步到 system-prompts Skills 目录。</small></label>
        </template>
        <template v-else-if="dialog === 'handbook'">
          <div v-if="loadingHandbookPackage" class="handbook-package-loading field--full"><LoaderCircle class="spin" :size="25" /><span>正在读取固定 Skills 文件</span></div>
          <div v-else class="handbook-editor-shell field--full">
            <div class="handbook-editor-meta">
              <div class="handbook-editor-fields">
                <label class="field"><span>手册类型</span><UiSelect v-model="handbookForm.handbook_type" :options="handbookTypeOptions" :disabled="Boolean(handbookForm.id)" /></label>
                <label class="field"><span>手册名称</span><input v-model="handbookForm.name" required placeholder="输入易于识别的手册名称" /></label>
                <label class="field field--full"><span>手册说明</span><textarea v-model="handbookForm.description" rows="3" placeholder="说明风格、叙事方法和适用项目"></textarea></label>
                <label class="check-field field--full" :data-disabled="editingHandbookUsedByProjects"><input v-model="handbookForm.enabled" type="checkbox" :disabled="editingHandbookUsedByProjects" /><span>发布此创作手册</span></label>
                <div v-if="editingHandbookUsedByProjects" class="config-protection-note field--full"><LockKeyhole :size="16" /><span>这本手册仍被项目使用。请先在相关项目中切换手册，再将其停用。</span></div>
              </div>
              <div class="handbook-cover-compact">
                <span>手册封面</span>
                <button class="handbook-cover-preview" type="button" title="选择手册封面" @click="handbookCoverInput?.click()">
                  <img :src="handbookCoverPreview" alt="手册封面预览" />
                  <span><ImagePlus :size="17" />{{ pendingHandbookCover ? '已选择' : '更换封面' }}</span>
                </button>
                <small>{{ pendingHandbookCover?.name || '建议 16:9，最大 8 MB' }}</small>
                <input ref="handbookCoverInput" class="sr-only" type="file" accept="image/jpeg,image/png,image/webp" @change="chooseHandbookCover" />
              </div>
            </div>
            <div class="handbook-files-heading"><div><Files :size="17" /><strong>固定 Skills 文件</strong><span>{{ validHandbookFileCount }}/{{ handbookFiles.length }} 已填写</span></div><small><LockKeyhole :size="13" />文件不可新增、删除或重命名</small></div>
            <div class="handbook-file-tabs" role="tablist" aria-label="手册固定文件">
              <button v-for="file in handbookFiles" :key="file.key" type="button" role="tab" :class="{ active: activeHandbookFile?.key === file.key, empty: !file.content.trim() }" :aria-selected="activeHandbookFile?.key === file.key" @click="activeHandbookFileKey = file.key"><Check v-if="file.content.trim()" :size="13" /><CircleAlert v-else :size="13" />{{ file.label }}</button>
            </div>
            <div v-if="activeHandbookFile" class="handbook-file-workspace">
              <section class="handbook-source-editor"><header><FileCode2 :size="16" /><div><strong>{{ activeHandbookFile.label }}</strong><code>{{ activeHandbookFile.filename }}</code></div></header><textarea :value="activeHandbookFile.content" spellcheck="false" @input="updateActiveHandbookFile(($event.target as HTMLTextAreaElement).value)"></textarea></section>
              <section class="handbook-preview"><header><Eye :size="16" /><div><strong>实时预览</strong><span>{{ activeHandbookFile.purpose }}</span></div></header><article class="markdown-body" v-html="handbookPreview"></article></section>
            </div>
          </div>
        </template>
      </form>
      <template #footer><span v-if="dialog === 'handbook'" class="dialog-managed-status" :data-complete="handbookFilesComplete"><FileCheck2 :size="15" />{{ handbookFilesComplete ? '固定文件完整' : '仍有文件未填写' }}</span><button class="button button--ghost" type="button" @click="closeDialog">取消</button><button class="button button--primary" type="submit" form="admin-form" :disabled="saving || loadingHandbookPackage || (dialog === 'handbook' && !handbookFilesComplete)"><LoaderCircle v-if="saving" class="spin" :size="17" /><Save v-else-if="dialog === 'handbook'" :size="17" />保存</button></template>
    </BaseDialog>

    <BaseDialog
      :open="Boolean(discoveryProvider)"
      :title="`${discoveryProvider?.name || ''} · 模型目录`"
      wide
      @update:open="!$event && (discoveryProvider = null)"
    >
      <div class="discovery-toolbar">
        <label class="search-control"><Search :size="17" /><input v-model="discoverySearch" placeholder="搜索模型 ID、名称或提供方" /></label>
        <button class="select-all-control" type="button" :aria-pressed="allDiscoverySelected" @click="toggleAllDiscovered">
          <span class="select-check"><Check v-if="allDiscoverySelected" :size="14" /></span>
          全选可导入模型
        </button>
      </div>
      <div v-if="discovering" class="discovery-loading"><LoaderCircle class="spin" :size="24" /><span>正在读取远程模型目录</span></div>
      <div v-else class="discovery-list">
        <article v-for="(row, index) in filteredDiscoveryRows" :key="row.model_id" v-motion="{ preset: 'row', index }" class="discovery-row" :class="{ imported: row.is_imported }">
          <button class="discovery-row__toggle" type="button" :disabled="row.is_imported" @click="row.selected = !row.selected">
            <span class="select-check" :data-selected="row.selected"><Check v-if="row.selected" :size="14" /></span>
          </button>
          <span class="discovery-row__icon"><component :is="modelTypeIcon[row.model_type]" :size="18" /></span>
          <div class="discovery-row__identity"><strong>{{ row.name }}</strong><span>{{ row.model_id }}</span><small v-if="row.owned_by">{{ row.owned_by }}</small></div>
          <span v-if="row.is_imported" class="default-chip"><Check :size="13" />已存在</span>
          <UiSelect v-else v-model="row.model_type" class="discovery-row__type" :options="modelTypeOptions" />
        </article>
        <div v-if="!filteredDiscoveryRows.length" class="activity-empty"><Search :size="24" /><span>没有匹配的模型</span></div>
      </div>
      <template #footer>
        <span class="dialog-selection-count">已选择 <strong class="tabular-nums">{{ selectedDiscoveryCount }}</strong> 个模型</span>
        <button class="button button--ghost" type="button" @click="discoveryProvider = null">取消</button>
        <button class="button button--primary" type="button" :disabled="!selectedDiscoveryCount || importing" @click="importDiscoveredModels"><LoaderCircle v-if="importing" class="spin" :size="17" /><Download v-else :size="17" />导入所选模型</button>
      </template>
    </BaseDialog>

    <BaseDialog
      :open="Boolean(deleteTarget)"
      title="确认删除配置"
      @update:open="!$event && (deleteTarget = null)"
    >
      <div class="danger-confirm">
        <span><Trash2 :size="22" /></span>
        <div><strong>删除“{{ deleteTarget?.name }}”</strong><p>系统会先检查项目、Agent 和历史任务引用。已被使用的配置不会被删除。</p></div>
      </div>
      <template #footer><button class="button button--ghost" type="button" @click="deleteTarget = null">取消</button><button class="button button--danger" type="button" :disabled="deleting" @click="confirmDelete"><LoaderCircle v-if="deleting" class="spin" :size="17" /><Trash2 v-else :size="17" />确认删除</button></template>
    </BaseDialog>
  </div>
</template>
