<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import {
  ArrowDownToLine,
  ArrowUpFromLine,
  AudioLines,
  Boxes,
  Check,
  ChevronDown,
  ChevronRight,
  Clock3,
  Download,
  GitBranchPlus,
  Grid2X2,
  Headphones,
  Image,
  Images,
  LayoutList,
  LoaderCircle,
  MapPinned,
  PackageSearch,
  Pause,
  Pencil,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  Save,
  Search,
  Sparkles,
  Trash2,
  Upload,
  UsersRound,
  WandSparkles,
  X,
} from 'lucide-vue-next'

import BaseDialog from '@/components/BaseDialog.vue'
import { api } from '@/lib/api'
import { useActivityStore } from '@/stores/activity'
import { useToastStore } from '@/stores/toast'
import type { AITask, AssetItem, AssetRevision, AssetType, PricingRule } from '@/types'

const props = defineProps<{
  projectId: string
  projectName: string
  projectAssets: AssetItem[]
  globalAssets: AssetItem[]
  pricing: PricingRule[]
}>()

const emit = defineEmits<{
  close: []
  assetsChanged: [projectAssets: AssetItem[], globalAssets: AssetItem[]]
}>()

type AssetScope = 'project' | 'global'
type ViewMode = 'list' | 'grid'
type AssetGroup = { root: AssetItem; children: AssetItem[] }

const activity = useActivityStore()
const toast = useToastStore()
const scope = ref<AssetScope>('project')
const typeFilter = ref<'all' | AssetType>('all')
const viewMode = ref<ViewMode>('list')
const search = ref('')
const selectedIds = ref<string[]>([])
const expandedIds = ref<string[]>([])
const localProjectAssets = ref<AssetItem[]>(props.projectAssets)
const localGlobalAssets = ref<AssetItem[]>(props.globalAssets)
const editorOpen = ref(false)
const editorAssetId = ref('')
const revisions = ref<AssetRevision[]>([])
const selectedRevisionId = ref('')
const loadingRevisions = ref(false)
const action = ref<
  'save' | 'prompt' | 'image' | 'upload' | 'audio_upload' | 'audio_remove' | 'restore' | 'transfer' | ''
>('')
const deleteTargets = ref<AssetItem[]>([])
const deleting = ref(false)
const imageInput = ref<HTMLInputElement | null>(null)
const audioInput = ref<HTMLInputElement | null>(null)
const audioPlayer = ref<HTMLAudioElement | null>(null)
const audioPlaying = ref(false)
const audioCurrentTime = ref(0)
const audioDuration = ref(0)
const pendingImage = ref<File | null>(null)
const pendingImagePreview = ref('')

const form = reactive({
  asset_type: 'character' as AssetType,
  parent_asset_id: '',
  name: '',
  description: '',
  generation_prompt: '',
})

const assetTypes: Array<{ value: 'all' | AssetType; label: string; icon: typeof Boxes }> = [
  { value: 'all', label: '全部资产', icon: Boxes },
  { value: 'character', label: '人物', icon: UsersRound },
  { value: 'scene', label: '场景', icon: MapPinned },
  { value: 'prop', label: '道具', icon: PackageSearch },
  { value: 'material', label: '素材', icon: Image },
  { value: 'audio', label: '音频', icon: Headphones },
]
const derivableTypes: AssetType[] = ['character', 'scene', 'prop']
const typeLabel: Record<AssetType, string> = { character: '人物', scene: '场景', prop: '道具', material: '素材', audio: '音频' }
const typeIcon = { character: UsersRound, scene: MapPinned, prop: PackageSearch, material: Image, audio: Headphones }
const statusLabel = { extracted: '待提示词', prompt_ready: '提示词就绪', generating: '生成中', ready: '已定稿', failed: '生成失败' }
const revisionLabel: Record<string, string> = {
  manual_create: '手动创建',
  manual_update: '手动编辑',
  manual_extraction: '手动提取',
  ai_extraction: 'AI 提取',
  ai_prompt_generation: 'AI 提示词',
  image_generation: 'AI 生图',
  image_upload: '用户上传',
  library_copy: '资产库复制',
  restore: '历史恢复',
  migration_snapshot: '历史基线',
}

watch(() => props.projectAssets, (value) => { localProjectAssets.value = value })
watch(() => props.globalAssets, (value) => { localGlobalAssets.value = value })
watch(scope, () => {
  selectedIds.value = []
  expandedIds.value = []
  closeEditor()
})

const sourceAssets = computed(() => scope.value === 'project' ? localProjectAssets.value : localGlobalAssets.value)
const editorAsset = computed(() => sourceAssets.value.find((asset) => asset.id === editorAssetId.value) ?? null)
const selectedRevision = computed(() => revisions.value.find((item) => item.id === selectedRevisionId.value) ?? revisions.value[0] ?? null)
const latestRevision = computed(() => revisions.value[0] ?? null)
const imageRevisions = computed(() => revisions.value.filter((item) => Boolean(item.media_url)))
const previewImage = computed(() => pendingImagePreview.value || selectedRevision.value?.media_url || editorAsset.value?.media_url || '')
const referenceAudioUrl = computed(() => metadataString(editorAsset.value, 'reference_audio_url'))
const referenceAudioName = computed(() => metadataString(editorAsset.value, 'reference_audio_filename'))
const referenceAudioMime = computed(() => metadataString(editorAsset.value, 'reference_audio_mime_type'))
const referenceAudioSize = computed(() => Number(editorAsset.value?.asset_metadata.reference_audio_size_bytes || 0))
const parentCandidates = computed(() => sourceAssets.value.filter((asset) => (
  !asset.parent_asset_id
  && asset.asset_type === form.asset_type
  && asset.id !== editorAssetId.value
)))
const activeTasks = computed(() => activity.tasks.filter((task) => (
  task.project_id === props.projectId
  && (task.status === 'queued' || task.status === 'running')
  && ['asset_prompt_generation', 'asset_image_generation'].includes(task.task_type)
)))
const busyAssetIds = computed(() => {
  const ids = new Set<string>()
  activeTasks.value.forEach((task) => {
    if (Array.isArray(task.request_payload.asset_ids)) {
      task.request_payload.asset_ids.forEach((id) => typeof id === 'string' && ids.add(id))
    }
    if (typeof task.request_payload.asset_id === 'string') ids.add(task.request_payload.asset_id)
  })
  return ids
})
const groups = computed<AssetGroup[]>(() => {
  const typed = sourceAssets.value.filter((asset) => typeFilter.value === 'all' || asset.asset_type === typeFilter.value)
  const ids = new Set(typed.map((asset) => asset.id))
  const query = search.value.trim().toLocaleLowerCase()
  const matches = (asset: AssetItem) => !query || `${asset.name} ${asset.description} ${asset.generation_prompt}`.toLocaleLowerCase().includes(query)
  const roots = typed.filter((asset) => !asset.parent_asset_id || !ids.has(asset.parent_asset_id))
  return roots.flatMap((root) => {
    const allChildren = typed.filter((asset) => asset.parent_asset_id === root.id)
    if (!matches(root) && !allChildren.some(matches)) return []
    return [{ root, children: query && !matches(root) ? allChildren.filter(matches) : allChildren }]
  })
})
const visibleAssets = computed(() => groups.value.flatMap((group) => [group.root, ...group.children]))
const allVisibleSelected = computed(() => Boolean(visibleAssets.value.length) && visibleAssets.value.every((asset) => selectedIds.value.includes(asset.id)))
const selectedAssets = computed(() => localProjectAssets.value.filter((asset) => selectedIds.value.includes(asset.id)))
const promptEligible = computed(() => selectedAssets.value.filter((asset) => !busyAssetIds.value.has(asset.id) && asset.status !== 'generating'))
const imageEligible = computed(() => selectedAssets.value.filter((asset) => (
  !busyAssetIds.value.has(asset.id)
  && asset.asset_type !== 'audio'
  && Boolean(asset.generation_prompt.trim())
)))
const formValid = computed(() => Boolean(form.name.trim()) && (!form.parent_asset_id || derivableTypes.includes(form.asset_type)))

function price(taskType: string, fallback: number): string {
  return Number(props.pricing.find((rule) => rule.task_type === taskType)?.unit_cost ?? fallback).toFixed(2)
}

function countType(type: 'all' | AssetType): number {
  return type === 'all' ? sourceAssets.value.length : sourceAssets.value.filter((asset) => asset.asset_type === type).length
}

function childrenOf(assetId: string): AssetItem[] {
  return sourceAssets.value.filter((asset) => asset.parent_asset_id === assetId)
}

function isExpanded(assetId: string): boolean {
  return expandedIds.value.includes(assetId)
}

function toggleExpanded(assetId: string): void {
  expandedIds.value = isExpanded(assetId)
    ? expandedIds.value.filter((id) => id !== assetId)
    : [...expandedIds.value, assetId]
}

function toggleSelection(assetId: string): void {
  if (scope.value !== 'project') return
  selectedIds.value = selectedIds.value.includes(assetId)
    ? selectedIds.value.filter((id) => id !== assetId)
    : [...selectedIds.value, assetId]
}

function toggleAllVisible(): void {
  const ids = visibleAssets.value.map((asset) => asset.id)
  selectedIds.value = allVisibleSelected.value
    ? selectedIds.value.filter((id) => !ids.includes(id))
    : [...new Set([...selectedIds.value, ...ids])]
}

function resetPendingImage(): void {
  if (pendingImagePreview.value) URL.revokeObjectURL(pendingImagePreview.value)
  pendingImage.value = null
  pendingImagePreview.value = ''
  if (imageInput.value) imageInput.value.value = ''
}

function metadataString(asset: AssetItem | null, key: string): string {
  const value = asset?.asset_metadata[key]
  return typeof value === 'string' ? value : ''
}

function resetAudioPlayer(): void {
  audioPlayer.value?.pause()
  audioPlaying.value = false
  audioCurrentTime.value = 0
  audioDuration.value = 0
  if (audioInput.value) audioInput.value.value = ''
}

function openEditor(asset?: AssetItem, parent?: AssetItem): void {
  resetPendingImage()
  resetAudioPlayer()
  editorAssetId.value = asset?.id ?? ''
  Object.assign(form, {
    asset_type: asset?.asset_type ?? parent?.asset_type ?? (typeFilter.value === 'all' ? 'character' : typeFilter.value),
    parent_asset_id: asset?.parent_asset_id ?? parent?.id ?? '',
    name: asset?.name ?? '',
    description: asset?.description ?? '',
    generation_prompt: asset?.generation_prompt ?? '',
  })
  revisions.value = []
  selectedRevisionId.value = ''
  editorOpen.value = true
  if (asset) void loadRevisions(asset.id)
}

function closeEditor(): void {
  editorOpen.value = false
  editorAssetId.value = ''
  revisions.value = []
  resetPendingImage()
  resetAudioPlayer()
}

async function loadRevisions(assetId: string, selectedId = ''): Promise<void> {
  loadingRevisions.value = true
  try {
    revisions.value = await api<AssetRevision[]>(`/assets/${assetId}/revisions`)
    selectedRevisionId.value = selectedId || revisions.value[0]?.id || ''
  } catch (error) {
    toast.show('图片历史读取失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    loadingRevisions.value = false
  }
}

async function refreshAssets(): Promise<void> {
  const [projectRows, globalRows] = await Promise.all([
    api<AssetItem[]>(`/projects/${props.projectId}/assets`),
    api<AssetItem[]>('/assets'),
  ])
  localProjectAssets.value = projectRows
  localGlobalAssets.value = globalRows
  selectedIds.value = selectedIds.value.filter((id) => projectRows.some((asset) => asset.id === id))
  emit('assetsChanged', projectRows, globalRows)
}

async function uploadImage(assetId: string, file: File): Promise<AssetItem> {
  const body = new FormData()
  body.set('file', file)
  return api<AssetItem>(`/assets/${assetId}/image/upload`, { method: 'POST', body })
}

function chooseImage(event: Event): void {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file) return
  resetPendingImage()
  pendingImage.value = file
  pendingImagePreview.value = URL.createObjectURL(file)
  if (editorAssetId.value) void uploadCurrentImage()
}

async function uploadCurrentImage(): Promise<void> {
  if (!editorAssetId.value || !pendingImage.value) return
  action.value = 'upload'
  try {
    const updated = await uploadImage(editorAssetId.value, pendingImage.value)
    resetPendingImage()
    await Promise.all([refreshAssets(), loadRevisions(updated.id)])
    toast.show('资产图片已上传', { message: '旧图片已保留在历史版本中', tone: 'success' })
  } catch (error) {
    toast.show('资产图片上传失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    action.value = ''
  }
}

async function chooseReferenceAudio(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file || !editorAssetId.value) return
  action.value = 'audio_upload'
  const body = new FormData()
  body.set('file', file)
  try {
    await api<AssetItem>(`/assets/${editorAssetId.value}/reference-audio/upload`, {
      method: 'POST',
      body,
    })
    resetAudioPlayer()
    await Promise.all([refreshAssets(), loadRevisions(editorAssetId.value)])
    toast.show('人物参考音频已绑定', {
      message: '支持音频输入的视频模型会自动携带该文件',
      tone: 'success',
    })
  } catch (error) {
    toast.show('参考音频上传失败', {
      message: error instanceof Error ? error.message : undefined,
      tone: 'error',
    })
  } finally {
    action.value = ''
    input.value = ''
  }
}

async function removeReferenceAudio(): Promise<void> {
  if (!editorAssetId.value || !referenceAudioUrl.value) return
  action.value = 'audio_remove'
  try {
    resetAudioPlayer()
    await api<AssetItem>(`/assets/${editorAssetId.value}/reference-audio`, {
      method: 'DELETE',
    })
    await Promise.all([refreshAssets(), loadRevisions(editorAssetId.value)])
    toast.show('人物参考音频已移除', { tone: 'success' })
  } catch (error) {
    toast.show('参考音频移除失败', {
      message: error instanceof Error ? error.message : undefined,
      tone: 'error',
    })
  } finally {
    action.value = ''
  }
}

async function toggleReferenceAudio(): Promise<void> {
  const player = audioPlayer.value
  if (!player) return
  if (player.paused) await player.play()
  else player.pause()
}

function seekReferenceAudio(event: Event): void {
  const player = audioPlayer.value
  if (!player) return
  const next = Number((event.target as HTMLInputElement).value)
  player.currentTime = next
  audioCurrentTime.value = next
}

function formatAudioTime(value: number): string {
  if (!Number.isFinite(value) || value < 0) return '00:00'
  const minutes = Math.floor(value / 60)
  const seconds = Math.floor(value % 60)
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
}

function fileSize(bytes: number): string {
  if (!bytes) return '0 KB'
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

async function saveAsset(closeAfter = false): Promise<AssetItem | null> {
  if (!formValid.value) return null
  action.value = 'save'
  try {
    const payload = {
      asset_type: form.asset_type,
      parent_asset_id: form.parent_asset_id || null,
      name: form.name.trim(),
      description: form.description.trim(),
      generation_prompt: form.generation_prompt.trim(),
    }
    const saved = editorAssetId.value
      ? await api<AssetItem>(`/assets/${editorAssetId.value}`, {
        method: 'PATCH',
        body: JSON.stringify({
          parent_asset_id: payload.parent_asset_id,
          name: payload.name,
          description: payload.description,
          generation_prompt: payload.generation_prompt,
        }),
      })
      : await api<AssetItem>(scope.value === 'project' ? `/projects/${props.projectId}/assets` : '/assets', {
        method: 'POST',
        body: JSON.stringify(payload),
      })
    editorAssetId.value = saved.id
    if (pendingImage.value) await uploadImage(saved.id, pendingImage.value)
    resetPendingImage()
    await Promise.all([refreshAssets(), loadRevisions(saved.id)])
    toast.show('资产已保存', { tone: 'success' })
    if (closeAfter) closeEditor()
    return saved
  } catch (error) {
    toast.show('资产保存失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
    return null
  } finally {
    action.value = ''
  }
}

async function queuePrompts(ids = promptEligible.value.map((asset) => asset.id)): Promise<void> {
  if (!ids.length || scope.value !== 'project') return
  action.value = 'prompt'
  try {
    await api<AITask>(`/projects/${props.projectId}/assets/prompts/generate`, {
      method: 'POST',
      body: JSON.stringify({ asset_ids: ids }),
    })
    await activity.refresh()
    toast.show(`${ids.length} 个提示词任务已排队`, { message: `${price('asset_prompt_generation', 2)} 积分/项`, tone: 'success' })
  } catch (error) {
    toast.show('提示词任务创建失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    action.value = ''
  }
}

async function queueCurrentPrompt(): Promise<void> {
  if (!editorAssetId.value) {
    const saved = await saveAsset(false)
    if (!saved) return
  }
  await queuePrompts([editorAssetId.value])
}

async function queueImages(ids = imageEligible.value.map((asset) => asset.id)): Promise<void> {
  if (!ids.length || scope.value !== 'project') return
  action.value = 'image'
  try {
    await api<AITask[]>(`/projects/${props.projectId}/assets/images/generate`, {
      method: 'POST',
      body: JSON.stringify({ asset_ids: ids }),
    })
    await Promise.all([activity.refresh(), refreshAssets()])
    toast.show(`${ids.length} 个生图任务已排队`, { message: `${price('asset_image_generation', 20)} 积分/项`, tone: 'success' })
  } catch (error) {
    toast.show('生图任务创建失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    action.value = ''
  }
}

async function queueCurrentImage(): Promise<void> {
  const saved = await saveAsset(false)
  if (!saved || !saved.generation_prompt.trim()) {
    toast.show('请先填写或生成生图提示词', { tone: 'error' })
    return
  }
  await queueImages([saved.id])
}

async function restoreRevision(): Promise<void> {
  const revision = selectedRevision.value
  if (!editorAssetId.value || !revision || revision.version === revisions.value[0]?.version) return
  action.value = 'restore'
  try {
    const restored = await api<AssetItem>(`/assets/${editorAssetId.value}/revisions/${revision.id}/restore`, { method: 'POST' })
    Object.assign(form, {
      parent_asset_id: restored.parent_asset_id ?? '',
      name: restored.name,
      description: restored.description,
      generation_prompt: restored.generation_prompt,
    })
    await Promise.all([refreshAssets(), loadRevisions(restored.id)])
    toast.show(`已恢复为 v${restored.version}`, { tone: 'success' })
  } catch (error) {
    toast.show('历史版本恢复失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    action.value = ''
  }
}

async function transferAsset(asset: AssetItem): Promise<void> {
  action.value = 'transfer'
  try {
    const path = asset.scope === 'project'
      ? `/projects/${props.projectId}/assets/${asset.id}/export-global`
      : `/projects/${props.projectId}/assets/import/${asset.id}`
    await api(path, { method: 'POST' })
    await refreshAssets()
    toast.show(asset.scope === 'project' ? '已复制到全局资产库' : '已导入项目塑造资产', {
      message: asset.parent_asset_id ? '基础资产关系已一并保留' : undefined,
      tone: 'success',
    })
  } catch (error) {
    toast.show('资产复制失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    action.value = ''
  }
}

function requestDelete(assets: AssetItem[]): void {
  deleteTargets.value = assets
}

async function confirmDelete(): Promise<void> {
  if (!deleteTargets.value.length) return
  deleting.value = true
  let deleted = 0
  const ordered = [...deleteTargets.value].sort((a, b) => Number(Boolean(b.parent_asset_id)) - Number(Boolean(a.parent_asset_id)))
  for (const asset of ordered) {
    try {
      await api(`/assets/${asset.id}`, { method: 'DELETE' })
      deleted += 1
    } catch (error) {
      toast.show(`“${asset.name}”删除失败`, { message: error instanceof Error ? error.message : undefined, tone: 'error' })
    }
  }
  deleteTargets.value = []
  deleting.value = false
  if (deleted) {
    closeEditor()
    await refreshAssets()
    toast.show(`已删除 ${deleted} 项资产`, { tone: 'success' })
  }
}

function downloadImage(url: string, name: string): void {
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${name}.webp`
  anchor.target = '_blank'
  anchor.rel = 'noopener'
  anchor.click()
}

function setAssetType(type: AssetType): void {
  if (editorAssetId.value) return
  form.asset_type = type
  if (!derivableTypes.includes(type)) form.parent_asset_id = ''
}

onBeforeUnmount(() => {
  resetPendingImage()
  resetAudioPlayer()
})
</script>

<template>
  <section class="asset-studio" :class="{ 'asset-studio--drawer': editorOpen }">
    <header class="asset-studio__topbar">
      <div class="asset-scope-switch" role="tablist" aria-label="资产库范围">
        <button type="button" :aria-selected="scope === 'project'" @click="scope = 'project'">
          <WandSparkles :size="18" /><span><strong>塑造资产</strong><small>AI 可直接读取</small></span><b class="tabular-nums">{{ localProjectAssets.length }}</b>
        </button>
        <button type="button" :aria-selected="scope === 'global'" @click="scope = 'global'">
          <Boxes :size="18" /><span><strong>全局资产</strong><small>租户共享素材</small></span><b class="tabular-nums">{{ localGlobalAssets.length }}</b>
        </button>
      </div>
      <div class="asset-studio__primary-actions">
        <button class="asset-action asset-action--primary" type="button" @click="openEditor()"><Plus :size="17" />新建资产</button>
      </div>
    </header>

    <nav class="asset-category-tabs" aria-label="资产分类">
      <button v-for="type in assetTypes" :key="type.value" type="button" :aria-pressed="typeFilter === type.value" @click="typeFilter = type.value">
        <component :is="type.icon" :size="16" /><span>{{ type.label }}</span><b class="tabular-nums">{{ countType(type.value) }}</b>
      </button>
    </nav>

    <div class="asset-commandbar">
      <label class="asset-search"><Search :size="16" /><input v-model="search" placeholder="搜索名称、说明或提示词" /><button v-if="search" type="button" title="清空搜索" @click="search = ''"><X :size="14" /></button></label>
      <div class="asset-view-switch" role="group" aria-label="视图模式">
        <button type="button" title="列表视图" :aria-pressed="viewMode === 'list'" @click="viewMode = 'list'"><LayoutList :size="17" /></button>
        <button type="button" title="卡片视图" :aria-pressed="viewMode === 'grid'" @click="viewMode = 'grid'"><Grid2X2 :size="16" /></button>
      </div>
      <template v-if="scope === 'project'">
        <button class="asset-action asset-action--quiet" type="button" :aria-pressed="allVisibleSelected" @click="toggleAllVisible"><Check :size="15" />{{ allVisibleSelected ? '取消全选' : '全选当前' }}</button>
        <button class="asset-action asset-action--quiet" type="button" :disabled="!selectedIds.length" @click="requestDelete(selectedAssets)"><Trash2 :size="15" />批量删除</button>
      </template>
    </div>

    <div v-if="scope === 'project' && selectedIds.length" class="asset-batch-dock">
      <span><Check :size="15" /><strong class="tabular-nums">{{ selectedIds.length }}</strong> 项已选择</span>
      <button type="button" :disabled="!promptEligible.length || Boolean(action)" @click="queuePrompts()"><Sparkles :size="16" />生成提示词 <b class="tabular-nums">{{ promptEligible.length }}</b><small>{{ price('asset_prompt_generation', 2) }} 积分/项</small></button>
      <button class="primary" type="button" :disabled="!imageEligible.length || Boolean(action)" @click="queueImages()"><Image :size="16" />批量生图 <b class="tabular-nums">{{ imageEligible.length }}</b><small>{{ price('asset_image_generation', 20) }} 积分/项</small></button>
      <button class="icon" type="button" title="取消选择" @click="selectedIds = []"><X :size="16" /></button>
    </div>

    <div class="asset-results" :data-view="viewMode">
      <template v-for="(group, groupIndex) in groups" :key="group.root.id">
        <article class="asset-family" :style="{ '--asset-index': groupIndex }">
          <div class="asset-record" :class="{ selected: selectedIds.includes(group.root.id), busy: busyAssetIds.has(group.root.id) }" @dblclick="openEditor(group.root)">
            <button v-if="group.children.length" class="asset-record__expand" type="button" :title="isExpanded(group.root.id) ? '收起衍生资产' : '展开衍生资产'" :aria-expanded="isExpanded(group.root.id)" @click="toggleExpanded(group.root.id)"><ChevronDown v-if="isExpanded(group.root.id)" :size="16" /><ChevronRight v-else :size="16" /></button>
            <span v-else class="asset-record__expand asset-record__expand--empty"></span>
            <button v-if="scope === 'project'" class="asset-check" type="button" :aria-label="`选择${group.root.name}`" :aria-pressed="selectedIds.includes(group.root.id)" @click="toggleSelection(group.root.id)"><Check :size="13" /></button>
            <button class="asset-record__preview" type="button" @click="openEditor(group.root)"><img v-if="group.root.media_url" :src="group.root.media_url" :alt="group.root.name" /><component :is="typeIcon[group.root.asset_type]" v-else :size="22" /><i v-if="busyAssetIds.has(group.root.id)"><LoaderCircle class="spin" :size="17" /></i></button>
            <div class="asset-record__identity"><strong>{{ group.root.name }}</strong><span><em>{{ typeLabel[group.root.asset_type] }}</em><i :data-status="group.root.status">{{ busyAssetIds.has(group.root.id) ? '处理中' : statusLabel[group.root.status] }}</i><small v-if="metadataString(group.root, 'reference_audio_url')"><AudioLines :size="12" />参考音频</small><small v-if="group.children.length"><GitBranchPlus :size="12" />{{ group.children.length }} 个衍生</small></span></div>
            <p class="asset-record__prompt">{{ group.root.generation_prompt || '尚未生成提示词，可手动填写或调用 AI 生成' }}</p>
            <p class="asset-record__description">{{ group.root.description || '暂无资产说明' }}</p>
            <div class="asset-record__actions">
              <button v-if="scope === 'project' && group.root.asset_type !== 'audio'" type="button" :disabled="busyAssetIds.has(group.root.id)" @click="group.root.generation_prompt ? queueImages([group.root.id]) : queuePrompts([group.root.id])"><WandSparkles :size="15" />{{ group.root.generation_prompt ? '生图' : '提示词' }}</button>
              <button type="button" @click="openEditor(group.root)"><Pencil :size="15" />编辑</button>
              <button type="button" title="复制到另一资产库" @click="transferAsset(group.root)"><ArrowUpFromLine v-if="scope === 'project'" :size="15" /><ArrowDownToLine v-else :size="15" /></button>
              <button class="danger" type="button" title="删除资产" @click="requestDelete([group.root])"><Trash2 :size="15" /></button>
            </div>
          </div>

          <div v-if="group.children.length && isExpanded(group.root.id)" class="asset-derivatives">
            <header><GitBranchPlus :size="14" /><span>{{ group.root.name }}的衍生资产</span><button type="button" @click="openEditor(undefined, group.root)"><Plus :size="14" />新增衍生</button></header>
            <div v-for="child in group.children" :key="child.id" class="asset-record asset-record--child" :class="{ selected: selectedIds.includes(child.id), busy: busyAssetIds.has(child.id) }">
              <span class="asset-record__expand asset-record__expand--empty"></span>
              <button v-if="scope === 'project'" class="asset-check" type="button" :aria-label="`选择${child.name}`" :aria-pressed="selectedIds.includes(child.id)" @click="toggleSelection(child.id)"><Check :size="13" /></button>
              <button class="asset-record__preview" type="button" @click="openEditor(child)"><img v-if="child.media_url" :src="child.media_url" :alt="child.name" /><component :is="typeIcon[child.asset_type]" v-else :size="20" /></button>
              <div class="asset-record__identity"><strong>{{ child.name }}</strong><span><em>衍生{{ typeLabel[child.asset_type] }}</em><i :data-status="child.status">{{ busyAssetIds.has(child.id) ? '处理中' : statusLabel[child.status] }}</i><small v-if="metadataString(child, 'reference_audio_url')"><AudioLines :size="12" />参考音频</small></span></div>
              <p class="asset-record__prompt">{{ child.generation_prompt || '尚未生成提示词' }}</p>
              <p class="asset-record__description">{{ child.description || '暂无衍生形态说明' }}</p>
              <div class="asset-record__actions"><button v-if="scope === 'project' && child.asset_type !== 'audio'" type="button" :disabled="busyAssetIds.has(child.id)" @click="child.generation_prompt ? queueImages([child.id]) : queuePrompts([child.id])"><WandSparkles :size="15" />{{ child.generation_prompt ? '生图' : '提示词' }}</button><button type="button" @click="openEditor(child)"><Pencil :size="15" />编辑</button><button class="danger" type="button" title="删除衍生资产" @click="requestDelete([child])"><Trash2 :size="15" /></button></div>
            </div>
          </div>
        </article>
      </template>
      <div v-if="!groups.length" class="asset-empty-state"><span><Boxes :size="28" /></span><strong>{{ search ? '没有匹配的资产' : '当前分类还没有资产' }}</strong><p>{{ scope === 'project' ? '从剧本提取、从全局资产导入，或手动建立第一个资产。' : '这里存放租户共享资产，项目需要导入后 AI 才会读取。' }}</p><button type="button" @click="openEditor()"><Plus :size="16" />新建资产</button></div>
    </div>

    <aside class="asset-editor-drawer" :aria-hidden="!editorOpen">
      <header><div><span>{{ editorAssetId ? '资产详情' : '新建资产' }}</span><strong>{{ form.name || '未命名资产' }}</strong></div><button type="button" title="关闭详情" @click="closeEditor"><X :size="18" /></button></header>
      <div class="asset-editor-drawer__scroll">
        <section class="asset-hero-preview">
          <div><img v-if="previewImage" :src="previewImage" :alt="form.name || '资产预览'" /><component :is="typeIcon[form.asset_type]" v-else :size="32" /><span v-if="action === 'upload'"><LoaderCircle class="spin" :size="21" />正在上传</span></div>
          <footer>
            <button type="button" :disabled="form.asset_type === 'audio' || action === 'upload'" @click="imageInput?.click()"><Upload :size="15" />上传图片</button>
            <button type="button" :disabled="!previewImage" @click="previewImage && downloadImage(previewImage, form.name || 'asset')"><Download :size="15" />下载</button>
            <input ref="imageInput" class="sr-only" type="file" accept="image/jpeg,image/png,image/webp" @change="chooseImage" />
          </footer>
        </section>

        <section v-if="editorAssetId" class="asset-image-history">
          <header><span><Images :size="15" />历史图片</span><small class="tabular-nums">{{ imageRevisions.length }} 张</small></header>
          <div v-if="loadingRevisions"><LoaderCircle class="spin" :size="18" />读取中</div>
          <div v-else-if="imageRevisions.length" class="asset-image-history__rail"><button v-for="revision in imageRevisions" :key="revision.id" type="button" :aria-pressed="selectedRevisionId === revision.id" :title="`v${revision.version} · ${revisionLabel[revision.change_type] || revision.change_type}`" @click="selectedRevisionId = revision.id"><img :src="revision.media_url!" :alt="`v${revision.version}`" /><span class="tabular-nums">v{{ revision.version }}</span></button></div>
          <p v-else>上传或生成图片后，历史版本会显示在这里。</p>
          <button v-if="selectedRevision && selectedRevision.version !== revisions[0]?.version" class="asset-restore-button" type="button" :disabled="action === 'restore'" @click="restoreRevision"><RotateCcw :size="15" />恢复当前选择为新版本</button>
        </section>

        <section v-if="form.asset_type === 'character'" class="asset-reference-audio">
          <header><span><AudioLines :size="16" />人物参考音频</span><small>视频声音身份</small></header>
          <template v-if="editorAssetId && referenceAudioUrl">
            <audio
              ref="audioPlayer"
              :src="referenceAudioUrl"
              preload="metadata"
              @loadedmetadata="audioDuration = audioPlayer?.duration || 0"
              @timeupdate="audioCurrentTime = audioPlayer?.currentTime || 0"
              @play="audioPlaying = true"
              @pause="audioPlaying = false"
              @ended="audioPlaying = false"
            ></audio>
            <div class="asset-reference-audio__player">
              <button type="button" :aria-label="audioPlaying ? '暂停参考音频' : '播放参考音频'" @click="toggleReferenceAudio">
                <Pause v-if="audioPlaying" :size="16" /><Play v-else :size="16" />
              </button>
              <div><strong>{{ referenceAudioName || '人物参考音频' }}</strong><span>{{ referenceAudioMime || '音频文件' }} · {{ fileSize(referenceAudioSize) }}</span></div>
              <span class="tabular-nums">{{ formatAudioTime(audioCurrentTime) }} / {{ formatAudioTime(audioDuration) }}</span>
            </div>
            <input class="asset-reference-audio__progress" type="range" min="0" :max="audioDuration || 0" step="0.05" :value="audioCurrentTime" aria-label="参考音频播放进度" @input="seekReferenceAudio" />
          </template>
          <p v-else>{{ editorAssetId ? '尚未绑定。支持音频参考的视频模型生成包含该人物的镜头时会自动使用。' : '请先保存人物资产，再上传参考音频。' }}</p>
          <footer>
            <button type="button" :disabled="!editorAssetId || action === 'audio_upload'" @click="audioInput?.click()"><LoaderCircle v-if="action === 'audio_upload'" class="spin" :size="15" /><Upload v-else :size="15" />{{ referenceAudioUrl ? '替换音频' : '上传音频' }}</button>
            <button v-if="referenceAudioUrl" class="danger" type="button" :disabled="action === 'audio_remove'" @click="removeReferenceAudio"><Trash2 :size="15" />移除</button>
            <input ref="audioInput" class="sr-only" type="file" accept="audio/mpeg,audio/wav,audio/x-wav,audio/mp4,audio/aac,audio/ogg,audio/webm,audio/flac" @change="chooseReferenceAudio" />
          </footer>
        </section>

        <form id="asset-workbench-form" class="asset-detail-form" @submit.prevent="saveAsset(false)">
          <div class="asset-detail-form__types"><button v-for="type in assetTypes.filter((item) => item.value !== 'all')" :key="type.value" type="button" :disabled="Boolean(editorAssetId)" :aria-pressed="form.asset_type === type.value" @click="setAssetType(type.value as AssetType)"><component :is="type.icon" :size="15" />{{ type.label }}</button></div>
          <label><span>资产名称</span><input v-model="form.name" required maxlength="160" placeholder="输入资产名称" /></label>
          <label><span>资产说明</span><textarea v-model="form.description" rows="4" placeholder="外形、身份、用途和需要保持的一致性"></textarea></label>
          <label v-if="derivableTypes.includes(form.asset_type)"><span>资产关系 <small>可选</small></span><select v-model="form.parent_asset_id" :disabled="Boolean(editorAsset && childrenOf(editorAsset.id).length)"><option value="">基础资产</option><option v-for="parent in parentCandidates" :key="parent.id" :value="parent.id">衍生自 · {{ parent.name }}</option></select><small v-if="form.parent_asset_id" class="asset-lineage-hint"><GitBranchPlus :size="13" />该资产会收纳在基础资产的二级列表中</small></label>
          <label class="asset-prompt-field"><span>生图提示词 <small>支持手动填写</small></span><textarea v-model="form.generation_prompt" rows="8" placeholder="可直接输入完整提示词，也可让 AI 根据资产说明和项目视觉手册生成"></textarea><button v-if="scope === 'project'" type="button" :disabled="action === 'prompt' || Boolean(busyAssetIds.has(editorAssetId))" @click="queueCurrentPrompt"><Sparkles :size="15" />{{ action === 'prompt' ? '任务创建中' : 'AI 生成提示词' }}<small>{{ price('asset_prompt_generation', 2) }} 积分</small></button></label>
        </form>

        <section v-if="editorAssetId && latestRevision" class="asset-version-meta"><Clock3 :size="15" /><span><strong>当前版本 v{{ latestRevision.version }}</strong><small>{{ revisionLabel[latestRevision.change_type] || latestRevision.change_type }} · {{ new Date(latestRevision.created_at).toLocaleString('zh-CN', { hour12: false }) }}</small></span></section>
      </div>
      <footer>
        <div v-if="editorAsset"><button type="button" title="复制到另一资产库" :disabled="action === 'transfer'" @click="transferAsset(editorAsset)"><ArrowUpFromLine v-if="scope === 'project'" :size="16" /><ArrowDownToLine v-else :size="16" /></button><button class="danger" type="button" title="删除资产" @click="requestDelete([editorAsset])"><Trash2 :size="16" /></button></div>
        <button type="submit" form="asset-workbench-form" :disabled="!formValid || Boolean(action)"><Save :size="16" />保存</button>
        <button v-if="scope === 'project' && form.asset_type !== 'audio'" class="primary" type="button" :disabled="!formValid || Boolean(action)" @click="queueCurrentImage"><RefreshCw v-if="editorAsset?.media_url" :size="16" /><WandSparkles v-else :size="16" />{{ editorAsset?.media_url ? '重新生成' : '生成图片' }}</button>
      </footer>
    </aside>
  </section>

  <BaseDialog :open="Boolean(deleteTargets.length)" title="确认删除资产" description="删除操作无法恢复，已复制到另一资产库的版本不受影响" @update:open="!$event && (deleteTargets = [])">
    <div class="asset-delete-confirm"><span><Trash2 :size="22" /></span><div><strong>删除 {{ deleteTargets.length }} 项资产</strong><p v-if="deleteTargets.some((asset) => childrenOf(asset.id).length)">选择中包含基础资产。必须同时选择并先删除其全部衍生资产。</p><p v-else>资产记录与全部版本历史会永久删除。</p></div></div>
    <template #footer><button class="asset-action asset-action--quiet" type="button" @click="deleteTargets = []">取消</button><button class="asset-action asset-action--danger" type="button" :disabled="deleting" @click="confirmDelete"><LoaderCircle v-if="deleting" class="spin" :size="16" /><Trash2 v-else :size="16" />确认删除</button></template>
  </BaseDialog>
</template>

<style scoped>
.asset-studio { --studio-line: rgb(21 31 36 / 9%); position: relative; display: grid; min-height: 0; height: 100%; grid-template-rows: auto auto auto minmax(0, 1fr); overflow: hidden; background: #f4f6f7; color: #172126; -webkit-font-smoothing: antialiased; }
.asset-studio:has(.asset-batch-dock) { grid-template-rows: auto auto auto auto minmax(0, 1fr); }
.asset-studio__topbar { display: flex; min-height: 76px; align-items: center; justify-content: space-between; gap: 18px; padding: 12px 18px; border-bottom: 1px solid var(--studio-line); background: rgb(255 255 255 / 90%); }
.asset-scope-switch { display: flex; gap: 6px; padding: 4px; border-radius: 10px; background: #edf0f1; box-shadow: inset 0 0 0 1px rgb(21 31 36 / 5%); }
.asset-scope-switch button { display: grid; min-width: 190px; min-height: 52px; grid-template-columns: 30px minmax(0, 1fr) auto; align-items: center; gap: 8px; padding: 6px 10px; border: 0; border-radius: 6px; color: #667278; background: transparent; cursor: pointer; text-align: left; transition-property: color, background-color, box-shadow, scale; transition-duration: 160ms; }
.asset-scope-switch button:active, .asset-action:active:not(:disabled), .asset-commandbar button:active:not(:disabled), .asset-record__actions button:active:not(:disabled), .asset-editor-drawer button:active:not(:disabled) { scale: .96; }
.asset-scope-switch button[aria-selected='true'] { color: #0d736f; background: #fff; box-shadow: 0 0 0 1px rgb(21 31 36 / 6%), 0 4px 12px rgb(21 31 36 / 7%); }
.asset-scope-switch strong, .asset-scope-switch small { display: block; }
.asset-scope-switch strong { color: #172126; font-size: 12px; }
.asset-scope-switch small { margin-top: 2px; font-size: 9px; }
.asset-scope-switch b { min-width: 25px; color: #0d736f; font-size: 12px; text-align: right; }
.asset-studio__primary-actions, .asset-record__actions, .asset-editor-drawer > footer, .asset-editor-drawer > footer > div { display: flex; align-items: center; gap: 6px; }
.asset-action { display: inline-flex; min-height: 40px; align-items: center; justify-content: center; gap: 7px; padding: 0 13px; border: 0; border-radius: 6px; cursor: pointer; font-size: 11px; font-weight: 700; transition-property: color, background-color, box-shadow, scale, opacity; transition-duration: 150ms; }
.asset-action--quiet { color: #536066; background: #fff; box-shadow: 0 0 0 1px rgb(21 31 36 / 10%); }
.asset-action--primary { color: #fff; background: #087f7a; box-shadow: 0 8px 18px rgb(8 127 122 / 18%); }
.asset-action--danger { color: #fff; background: #c94747; }
.asset-action:disabled { cursor: not-allowed; opacity: .45; }
.asset-category-tabs { display: flex; min-height: 52px; gap: 2px; overflow-x: auto; padding: 0 16px; border-bottom: 1px solid var(--studio-line); background: #fff; scrollbar-width: none; }
.asset-category-tabs button { position: relative; display: inline-flex; min-width: max-content; min-height: 52px; align-items: center; gap: 7px; padding: 0 12px; border: 0; color: #798489; background: transparent; cursor: pointer; font-size: 11px; font-weight: 650; transition-property: color, background-color, scale; transition-duration: 150ms; }
.asset-category-tabs button::after { position: absolute; right: 11px; bottom: 0; left: 11px; height: 2px; content: ''; background: transparent; }
.asset-category-tabs button[aria-pressed='true'] { color: #087f7a; }
.asset-category-tabs button[aria-pressed='true']::after { background: #087f7a; }
.asset-category-tabs b { min-width: 21px; height: 20px; padding: 0 5px; border-radius: 5px; color: #7a858a; background: #f0f2f3; font-size: 9px; line-height: 20px; text-align: center; }
.asset-commandbar { display: flex; min-height: 58px; align-items: center; gap: 7px; padding: 9px 16px; border-bottom: 1px solid var(--studio-line); background: #fbfcfc; }
.asset-search { display: grid; min-width: 220px; max-width: 430px; height: 40px; flex: 1; grid-template-columns: 28px minmax(0, 1fr) 34px; align-items: center; padding-left: 10px; border-radius: 6px; background: #fff; box-shadow: inset 0 0 0 1px rgb(21 31 36 / 12%); }
.asset-search > svg { color: #8a9498; }
.asset-search input { min-width: 0; border: 0; outline: 0; color: #172126; background: transparent; font-size: 11px; }
.asset-search button, .asset-view-switch button, .asset-commandbar > button { display: inline-flex; min-width: 40px; min-height: 40px; align-items: center; justify-content: center; gap: 6px; padding: 0 10px; border: 0; border-radius: 6px; color: #667278; background: #fff; box-shadow: 0 0 0 1px rgb(21 31 36 / 10%); cursor: pointer; font-size: 10px; transition-property: color, background-color, box-shadow, scale, opacity; transition-duration: 150ms; }
.asset-view-switch { display: flex; gap: 2px; padding: 3px; border-radius: 7px; background: #e9edef; }
.asset-view-switch button { min-height: 34px; width: 36px; min-width: 36px; padding: 0; background: transparent; box-shadow: none; }
.asset-view-switch button[aria-pressed='true'] { color: #087f7a; background: #fff; box-shadow: 0 0 0 1px rgb(21 31 36 / 6%), 0 2px 7px rgb(21 31 36 / 6%); }
.asset-commandbar > button[aria-pressed='true'] { color: #087f7a; background: #e9f6f4; box-shadow: 0 0 0 1px rgb(8 127 122 / 18%); }
.asset-commandbar button:disabled { cursor: not-allowed; opacity: .4; }
.asset-batch-dock { display: flex; min-height: 54px; align-items: center; gap: 7px; padding: 7px 16px; color: #0b625f; background: #e8f5f3; box-shadow: inset 0 -1px rgb(8 127 122 / 10%); }
.asset-batch-dock > span { display: inline-flex; min-width: 135px; align-items: center; gap: 6px; font-size: 10px; }
.asset-batch-dock > span strong { font-size: 13px; }
.asset-batch-dock button { display: inline-flex; min-height: 38px; align-items: center; gap: 6px; padding: 0 11px; border: 0; border-radius: 6px; color: #0b625f; background: #fff; box-shadow: 0 0 0 1px rgb(8 127 122 / 16%); cursor: pointer; font-size: 10px; font-weight: 700; transition-property: color, background-color, box-shadow, scale, opacity; transition-duration: 150ms; }
.asset-batch-dock button.primary { color: #fff; background: #087f7a; }
.asset-batch-dock button.icon { min-width: 40px; margin-left: auto; padding: 0; }
.asset-batch-dock button small { opacity: .68; font-size: 8px; }
.asset-batch-dock button:disabled { cursor: not-allowed; opacity: .45; }
.asset-results { min-height: 0; overflow: auto; padding: 12px 16px 22px; }
.asset-family { display: grid; min-width: 0; align-self: start; animation: asset-family-in 300ms cubic-bezier(.2,0,0,1) both; animation-delay: min(calc(var(--asset-index) * 35ms), 210ms); }
@keyframes asset-family-in { from { opacity: 0; transform: translateY(10px); filter: blur(4px); } }
.asset-results[data-view='list'] .asset-family + .asset-family { border-top: 1px solid rgb(21 31 36 / 7%); }
.asset-record { display: grid; min-width: 0; min-height: 94px; grid-template-columns: 32px 42px 92px minmax(130px, .7fr) minmax(180px, 1.35fr) minmax(160px, 1fr) auto; align-items: center; gap: 10px; padding: 10px 10px 10px 4px; background: transparent; transition-property: background-color, box-shadow, transform; transition-duration: 160ms; }
.asset-record:hover { position: relative; z-index: 1; background: #fff; box-shadow: 0 0 0 1px rgb(21 31 36 / 6%), 0 7px 18px rgb(21 31 36 / 6%); }
.asset-record.selected { background: #eef8f6; box-shadow: inset 3px 0 #087f7a; }
.asset-record.busy { background: #f3f8f7; }
.asset-record--child { margin-left: 38px; min-height: 82px; border-top: 1px solid rgb(21 31 36 / 6%); }
.asset-record__expand { display: inline-flex; width: 40px; height: 40px; align-items: center; justify-content: center; border: 0; border-radius: 6px; color: #7b878b; background: transparent; cursor: pointer; transition-property: color, background-color, scale; transition-duration: 150ms; }
.asset-record__expand:hover { color: #087f7a; background: #e9f6f4; }
.asset-record__expand--empty { width: 32px; height: 40px; }
.asset-check { position: relative; display: inline-flex; width: 40px; height: 40px; align-items: center; justify-content: center; border: 0; color: transparent; background: transparent; cursor: pointer; }
.asset-check::before { position: absolute; width: 19px; height: 19px; border-radius: 5px; content: ''; background: #fff; box-shadow: 0 0 0 1px rgb(21 31 36 / 20%); transition-property: background-color, box-shadow, scale; transition-duration: 160ms; }
.asset-check svg { position: relative; opacity: 0; scale: .25; filter: blur(4px); transition-property: color, opacity, scale, filter; transition-duration: 300ms; transition-timing-function: cubic-bezier(.2,0,0,1); }
.asset-check[aria-pressed='true']::before { background: #087f7a; box-shadow: 0 0 0 1px #087f7a; }
.asset-check[aria-pressed='true'] svg { color: #fff; opacity: 1; scale: 1; filter: blur(0); }
.asset-record__preview { position: relative; display: flex; width: 92px; height: 70px; align-items: center; justify-content: center; overflow: hidden; border: 0; border-radius: 6px; color: #087f7a; background: #e7efef; cursor: pointer; box-shadow: 0 0 0 1px rgb(21 31 36 / 8%); }
.asset-record__preview img { width: 100%; height: 100%; object-fit: cover; outline: 1px solid rgb(0 0 0 / 10%); outline-offset: -1px; }
.asset-record__preview i { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; color: #fff; background: rgb(18 28 31 / 50%); }
.asset-record__identity { min-width: 0; }
.asset-record__identity > strong { display: block; overflow: hidden; font-size: 11px; text-overflow: ellipsis; text-wrap: balance; white-space: nowrap; }
.asset-record__identity > span { display: flex; align-items: center; gap: 5px; margin-top: 7px; }
.asset-record__identity em, .asset-record__identity i { padding: 3px 5px; border-radius: 4px; font-size: 8px; font-style: normal; }
.asset-record__identity em { color: #91580a; background: #fff1db; }
.asset-record__identity i { color: #8a641e; background: #fff8e7; }
.asset-record__identity i[data-status='ready'] { color: #08705e; background: #e5f6ef; }
.asset-record__identity i[data-status='failed'] { color: #b54141; background: #faeaea; }
.asset-record__identity small { display: inline-flex; align-items: center; gap: 3px; color: #0d736f; font-size: 8px; }
.asset-record__prompt, .asset-record__description { display: -webkit-box; min-width: 0; overflow: hidden; color: #556267; font-size: 9px; line-height: 1.55; text-wrap: pretty; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
.asset-record__prompt { color: #304044; }
.asset-record__actions { justify-content: flex-end; }
.asset-record__actions button { display: inline-flex; min-width: 40px; min-height: 40px; align-items: center; justify-content: center; gap: 5px; padding: 0 8px; border: 0; border-radius: 6px; color: #087f7a; background: transparent; cursor: pointer; font-size: 9px; font-weight: 700; transition-property: color, background-color, scale, opacity; transition-duration: 150ms; }
.asset-record__actions button:hover { background: #e9f6f4; }
.asset-record__actions button.danger { color: #c94747; }
.asset-record__actions button.danger:hover { background: #faeaea; }
.asset-record__actions button:disabled { cursor: not-allowed; opacity: .4; }
.asset-derivatives { margin: 0 0 8px 28px; overflow: hidden; border-radius: 8px; background: #f0f3f3; box-shadow: inset 3px 0 #92b9b5, inset 0 0 0 1px rgb(21 31 36 / 5%); }
.asset-derivatives > header { display: flex; min-height: 40px; align-items: center; gap: 6px; padding: 0 12px 0 50px; color: #607075; font-size: 9px; font-weight: 700; }
.asset-derivatives > header button { display: inline-flex; min-height: 34px; align-items: center; gap: 5px; margin-left: auto; padding: 0 8px; border: 0; border-radius: 5px; color: #087f7a; background: transparent; cursor: pointer; font-size: 9px; }
.asset-results[data-view='grid'] { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); grid-auto-rows: max-content; align-content: start; align-items: start; gap: 14px; }
.asset-results[data-view='grid'] .asset-family { height: max-content; overflow: hidden; border-radius: 8px; background: #fff; box-shadow: 0 0 0 1px rgb(21 31 36 / 8%), 0 8px 22px rgb(21 31 36 / 6%); }
.asset-results[data-view='grid'] .asset-family > .asset-record { position: relative; min-height: 348px; grid-template-columns: minmax(0, 1fr); grid-template-rows: auto auto minmax(44px, auto) minmax(32px, auto) auto; align-content: start; align-items: start; gap: 9px; padding: 10px; }
.asset-results[data-view='grid'] .asset-family > .asset-record:hover { transform: translateY(-2px); }
.asset-results[data-view='grid'] .asset-family > .asset-record > .asset-record__expand { position: absolute; z-index: 2; top: 14px; left: 14px; margin: 0; background: rgb(255 255 255 / 92%); box-shadow: 0 2px 8px rgb(21 31 36 / 10%); backdrop-filter: blur(8px); }
.asset-results[data-view='grid'] .asset-family > .asset-record > .asset-record__expand--empty { display: none; }
.asset-results[data-view='grid'] .asset-family > .asset-record > .asset-check { position: absolute; z-index: 2; top: 14px; right: 14px; margin: 0; border-radius: 6px; background: rgb(255 255 255 / 92%); box-shadow: 0 2px 8px rgb(21 31 36 / 10%); backdrop-filter: blur(8px); }
.asset-results[data-view='grid'] .asset-family > .asset-record > .asset-record__preview { width: 100%; height: auto; aspect-ratio: 16 / 9; grid-column: 1; border-radius: 6px; }
.asset-results[data-view='grid'] .asset-family > .asset-record > .asset-record__identity,
.asset-results[data-view='grid'] .asset-family > .asset-record > .asset-record__prompt,
.asset-results[data-view='grid'] .asset-family > .asset-record > .asset-record__description,
.asset-results[data-view='grid'] .asset-family > .asset-record > .asset-record__actions { width: 100%; grid-column: 1; }
.asset-results[data-view='grid'] .asset-family > .asset-record > .asset-record__identity { padding: 2px 2px 0; }
.asset-results[data-view='grid'] .asset-family > .asset-record > .asset-record__identity > strong { font-size: 13px; }
.asset-results[data-view='grid'] .asset-family > .asset-record > .asset-record__prompt,
.asset-results[data-view='grid'] .asset-family > .asset-record > .asset-record__description { padding-inline: 2px; font-size: 10px; }
.asset-results[data-view='grid'] .asset-family > .asset-record > .asset-record__actions { min-height: 46px; justify-content: flex-start; border-top: 1px solid rgb(21 31 36 / 7%); padding-top: 6px; }
.asset-results[data-view='grid'] .asset-family > .asset-record > .asset-record__actions .danger { margin-left: auto; }
.asset-results[data-view='grid'] .asset-derivatives { margin: 0; border-radius: 0; box-shadow: inset 0 1px rgb(21 31 36 / 7%); }
.asset-results[data-view='grid'] .asset-derivatives > header { padding-left: 12px; }
.asset-results[data-view='grid'] .asset-record--child { position: relative; min-height: 142px; margin: 0; grid-template-columns: 64px minmax(0, 1fr); grid-template-rows: auto auto auto; align-items: center; gap: 7px 10px; padding: 10px; }
.asset-results[data-view='grid'] .asset-record--child .asset-record__expand--empty { display: none; }
.asset-results[data-view='grid'] .asset-record--child .asset-check { position: absolute; z-index: 2; top: 13px; left: 13px; margin: 0; border-radius: 5px; background: rgb(255 255 255 / 90%); }
.asset-results[data-view='grid'] .asset-record--child .asset-record__preview { width: 64px; height: 64px; grid-column: 1; grid-row: 1 / 3; border-radius: 6px; }
.asset-results[data-view='grid'] .asset-record--child .asset-record__identity { grid-column: 2; grid-row: 1; }
.asset-results[data-view='grid'] .asset-record--child .asset-record__prompt { grid-column: 2; grid-row: 2; -webkit-line-clamp: 2; }
.asset-results[data-view='grid'] .asset-record--child .asset-record__description { display: none; }
.asset-results[data-view='grid'] .asset-record--child .asset-record__actions { min-height: 42px; grid-column: 1 / -1; grid-row: 3; justify-content: flex-start; border-top: 1px solid rgb(21 31 36 / 7%); padding-top: 5px; }
.asset-results[data-view='grid'] .asset-record--child .asset-record__actions .danger { margin-left: auto; }
.asset-empty-state { display: flex; min-height: 360px; grid-column: 1 / -1; flex-direction: column; align-items: center; justify-content: center; gap: 7px; color: #7a858a; text-align: center; }
.asset-empty-state > span { display: flex; width: 58px; height: 58px; align-items: center; justify-content: center; border-radius: 50%; color: #087f7a; background: #e6f3f1; }
.asset-empty-state strong { margin-top: 5px; color: #344247; font-size: 13px; }
.asset-empty-state p { max-width: 380px; font-size: 10px; line-height: 1.65; text-wrap: pretty; }
.asset-empty-state button { display: inline-flex; min-height: 40px; align-items: center; gap: 6px; margin-top: 5px; padding: 0 12px; border: 0; border-radius: 6px; color: #fff; background: #087f7a; cursor: pointer; }
.asset-editor-drawer { position: absolute; z-index: 10; top: 0; right: 0; bottom: 0; display: grid; width: min(470px, 46vw); grid-template-rows: auto minmax(0, 1fr) auto; background: #fff; box-shadow: -20px 0 48px rgb(21 31 36 / 16%), -1px 0 rgb(21 31 36 / 8%); transform: translateX(104%); opacity: 0; pointer-events: none; transition-property: transform, opacity; transition-duration: 240ms; transition-timing-function: cubic-bezier(.2,0,0,1); }
.asset-studio--drawer .asset-editor-drawer { transform: translateX(0); opacity: 1; pointer-events: auto; }
.asset-editor-drawer > header { display: flex; min-height: 66px; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 14px 10px 18px; border-bottom: 1px solid var(--studio-line); }
.asset-editor-drawer > header span, .asset-editor-drawer > header strong { display: block; }
.asset-editor-drawer > header span { color: #798489; font-size: 9px; }
.asset-editor-drawer > header strong { margin-top: 3px; font-size: 13px; text-wrap: balance; }
.asset-editor-drawer > header button { display: inline-flex; width: 40px; height: 40px; align-items: center; justify-content: center; border: 0; border-radius: 6px; color: #6c787d; background: transparent; cursor: pointer; }
.asset-editor-drawer__scroll { min-height: 0; overflow-y: auto; padding: 14px 18px 22px; }
.asset-hero-preview { overflow: hidden; border-radius: 8px; background: #eef2f3; box-shadow: 0 0 0 1px rgb(21 31 36 / 8%); }
.asset-hero-preview > div { position: relative; display: flex; width: 100%; aspect-ratio: 16 / 8.6; align-items: center; justify-content: center; color: #087f7a; }
.asset-hero-preview img { width: 100%; height: 100%; object-fit: contain; outline: 1px solid rgb(0 0 0 / 10%); outline-offset: -1px; }
.asset-hero-preview > div > span { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; gap: 7px; color: #fff; background: rgb(21 31 36 / 54%); font-size: 10px; }
.asset-hero-preview footer { display: flex; justify-content: flex-end; gap: 3px; padding: 4px; background: #fff; }
.asset-hero-preview footer button, .asset-restore-button { display: inline-flex; min-height: 38px; align-items: center; gap: 6px; padding: 0 9px; border: 0; border-radius: 5px; color: #087f7a; background: transparent; cursor: pointer; font-size: 9px; font-weight: 700; transition-property: color, background-color, scale, opacity; transition-duration: 150ms; }
.asset-hero-preview footer button:hover { background: #e9f6f4; }
.asset-hero-preview footer button:disabled { cursor: not-allowed; opacity: .4; }
.asset-reference-audio { margin-top: 12px; padding: 12px; border-radius: 10px; background: #f7f9f9; box-shadow: 0 0 0 1px rgb(21 31 36 / 7%), 0 4px 14px rgb(21 31 36 / 4%); }
.asset-reference-audio > header { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.asset-reference-audio > header > span { display: inline-flex; align-items: center; gap: 7px; color: #26363b; font-size: 11px; font-weight: 750; }
.asset-reference-audio > header > span svg { color: #087f7a; }
.asset-reference-audio > header small { color: #879196; font-size: 9px; }
.asset-reference-audio > p { margin: 12px 0 2px; color: #748087; font-size: 10px; line-height: 1.65; text-wrap: pretty; }
.asset-reference-audio > audio { display: none; }
.asset-reference-audio__player { display: grid; grid-template-columns: 40px minmax(0, 1fr) auto; align-items: center; gap: 9px; margin-top: 11px; }
.asset-reference-audio__player > button { display: grid; width: 40px; height: 40px; place-items: center; border: 0; border-radius: 8px; color: #fff; background: #087f7a; box-shadow: 0 7px 16px rgb(8 127 122 / 20%); cursor: pointer; transition-property: scale, box-shadow; transition-duration: 150ms; }
.asset-reference-audio__player > button:active { scale: .96; }
.asset-reference-audio__player > button svg { margin-left: 1px; }
.asset-reference-audio__player > div { min-width: 0; }
.asset-reference-audio__player strong, .asset-reference-audio__player div > span { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.asset-reference-audio__player strong { color: #253338; font-size: 10px; }
.asset-reference-audio__player div > span { margin-top: 3px; color: #899398; font-size: 8px; }
.asset-reference-audio__player > span { color: #6f7b80; font-size: 9px; }
.asset-reference-audio__progress { width: 100%; height: 18px; margin: 7px 0 0; accent-color: #087f7a; cursor: pointer; }
.asset-reference-audio > footer { display: flex; justify-content: flex-end; gap: 5px; margin-top: 8px; }
.asset-reference-audio > footer button { display: inline-flex; min-height: 40px; align-items: center; gap: 6px; padding: 0 10px; border: 0; border-radius: 6px; color: #087f7a; background: #e9f4f3; cursor: pointer; font-size: 9px; font-weight: 750; transition-property: color, background-color, scale, opacity; transition-duration: 150ms; }
.asset-reference-audio > footer button.danger { color: #c94747; background: #faeeee; }
.asset-reference-audio > footer button:active:not(:disabled) { scale: .96; }
.asset-reference-audio > footer button:disabled { cursor: not-allowed; opacity: .45; }
.asset-image-history { display: grid; gap: 8px; margin-top: 16px; }
.asset-image-history > header { display: flex; align-items: center; justify-content: space-between; color: #435156; font-size: 10px; font-weight: 700; }
.asset-image-history > header span { display: inline-flex; align-items: center; gap: 6px; }
.asset-image-history > header small { color: #869095; }
.asset-image-history > div:not(.asset-image-history__rail) { display: flex; min-height: 70px; align-items: center; justify-content: center; gap: 6px; color: #7a858a; font-size: 9px; }
.asset-image-history > p { color: #849095; font-size: 9px; }
.asset-image-history__rail { display: flex; gap: 7px; overflow-x: auto; padding: 2px 1px 5px; scrollbar-width: thin; }
.asset-image-history__rail button { position: relative; width: 84px; height: 68px; flex: 0 0 auto; overflow: hidden; border: 0; border-radius: 6px; background: #edf1f2; cursor: pointer; box-shadow: 0 0 0 1px rgb(21 31 36 / 9%); transition-property: box-shadow, scale; transition-duration: 150ms; }
.asset-image-history__rail button[aria-pressed='true'] { box-shadow: 0 0 0 2px #087f7a, 0 5px 12px rgb(8 127 122 / 14%); }
.asset-image-history__rail img { width: 100%; height: 100%; object-fit: cover; outline: 1px solid rgb(0 0 0 / 10%); outline-offset: -1px; }
.asset-image-history__rail span { position: absolute; right: 4px; bottom: 4px; padding: 2px 4px; border-radius: 3px; color: #fff; background: rgb(21 31 36 / 68%); font-size: 7px; }
.asset-restore-button { justify-content: center; color: #49575c; background: #f1f3f4; }
.asset-detail-form { display: grid; gap: 13px; margin-top: 18px; }
.asset-detail-form__types { display: grid; grid-template-columns: repeat(5, 1fr); gap: 4px; }
.asset-detail-form__types button { display: flex; min-width: 0; min-height: 42px; flex-direction: column; align-items: center; justify-content: center; gap: 3px; border: 0; border-radius: 6px; color: #778388; background: #f1f3f4; cursor: pointer; font-size: 8px; transition-property: color, background-color, box-shadow, scale, opacity; transition-duration: 150ms; }
.asset-detail-form__types button[aria-pressed='true'] { color: #087f7a; background: #e7f4f2; box-shadow: inset 0 0 0 1px rgb(8 127 122 / 16%); }
.asset-detail-form__types button:disabled { cursor: default; opacity: .6; }
.asset-detail-form label { display: grid; gap: 6px; color: #445156; font-size: 10px; font-weight: 700; }
.asset-detail-form label > span { display: flex; align-items: center; justify-content: space-between; }
.asset-detail-form label > span small { color: #8a9498; font-size: 8px; font-weight: 500; }
.asset-detail-form input, .asset-detail-form textarea, .asset-detail-form select { width: 100%; border: 1px solid rgb(21 31 36 / 13%); border-radius: 6px; outline: 0; color: #1b272c; background: #fbfcfc; font: inherit; font-weight: 500; transition-property: border-color, background-color, box-shadow; transition-duration: 150ms; }
.asset-detail-form input, .asset-detail-form select { min-height: 42px; padding: 0 11px; }
.asset-detail-form textarea { resize: vertical; padding: 10px 11px; line-height: 1.65; }
.asset-detail-form input:focus, .asset-detail-form textarea:focus, .asset-detail-form select:focus { border-color: rgb(8 127 122 / 55%); background: #fff; box-shadow: 0 0 0 3px rgb(8 127 122 / 9%); }
.asset-lineage-hint { display: flex; align-items: center; gap: 5px; color: #087f7a; font-size: 8px; font-weight: 500; }
.asset-prompt-field { position: relative; }
.asset-prompt-field textarea { padding-bottom: 48px; }
.asset-prompt-field > button { position: absolute; right: 7px; bottom: 7px; display: inline-flex; min-height: 34px; align-items: center; gap: 6px; padding: 0 9px; border: 0; border-radius: 5px; color: #087f7a; background: #e7f4f2; cursor: pointer; font-size: 9px; font-weight: 700; transition-property: color, background-color, scale, opacity; transition-duration: 150ms; }
.asset-prompt-field > button small { opacity: .65; font-size: 7px; }
.asset-version-meta { display: flex; align-items: center; gap: 9px; margin-top: 16px; padding: 10px; border-radius: 7px; color: #5d696e; background: #f1f3f4; }
.asset-version-meta strong, .asset-version-meta small { display: block; }
.asset-version-meta strong { font-size: 9px; }
.asset-version-meta small { margin-top: 2px; color: #8a9498; font-size: 8px; }
.asset-editor-drawer > footer { min-height: 66px; justify-content: flex-end; padding: 9px 14px; border-top: 1px solid var(--studio-line); background: #fff; }
.asset-editor-drawer > footer > div { margin-right: auto; }
.asset-editor-drawer > footer button { display: inline-flex; min-width: 40px; min-height: 42px; align-items: center; justify-content: center; gap: 6px; padding: 0 12px; border: 0; border-radius: 6px; color: #4e5c61; background: #f0f2f3; cursor: pointer; font-size: 10px; font-weight: 700; transition-property: color, background-color, box-shadow, scale, opacity; transition-duration: 150ms; }
.asset-editor-drawer > footer button.primary { color: #fff; background: #087f7a; box-shadow: 0 8px 18px rgb(8 127 122 / 18%); }
.asset-editor-drawer > footer button.danger { color: #c94747; }
.asset-editor-drawer > footer button:disabled { cursor: not-allowed; opacity: .45; }
.asset-delete-confirm { display: grid; grid-template-columns: 48px minmax(0, 1fr); align-items: start; gap: 12px; }
.asset-delete-confirm > span { display: flex; width: 48px; height: 48px; align-items: center; justify-content: center; border-radius: 8px; color: #c94747; background: #faeaea; }
.asset-delete-confirm strong { font-size: 12px; }
.asset-delete-confirm p { margin-top: 6px; color: #6d797e; font-size: 10px; line-height: 1.6; text-wrap: pretty; }
@media (max-width: 980px) {
  .asset-scope-switch button { min-width: 150px; }
  .asset-results[data-view='grid'] { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .asset-record { grid-template-columns: 32px 42px 82px minmax(120px, .7fr) minmax(150px, 1fr) auto; }
  .asset-record__description { display: none; }
  .asset-editor-drawer { width: min(500px, 64vw); }
}
@media (max-width: 700px) {
  .asset-studio { height: 100%; min-height: 0; grid-template-rows: auto auto auto minmax(0, 1fr); }
  .asset-studio:has(.asset-batch-dock) { grid-template-rows: auto auto auto auto minmax(0, 1fr); }
  .asset-studio__topbar { display: grid; min-height: 72px; grid-template-columns: minmax(0, 1fr) 42px; align-items: center; gap: 8px; padding: 8px 10px; }
  .asset-scope-switch { display: grid; grid-template-columns: 1fr 1fr; }
  .asset-scope-switch button { min-width: 0; min-height: 44px; grid-template-columns: 22px minmax(0, 1fr) auto; gap: 5px; padding: 4px 7px; }
  .asset-scope-switch button > svg { width: 16px; height: 16px; }
  .asset-scope-switch small { display: none; }
  .asset-scope-switch strong { overflow: hidden; font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
  .asset-scope-switch b { min-width: 16px; font-size: 9px; }
  .asset-studio__primary-actions { justify-content: flex-end; }
  .asset-studio__primary-actions .asset-action { width: 42px; min-width: 42px; height: 42px; padding: 0; font-size: 0; }
  .asset-category-tabs { min-height: 48px; padding-inline: 8px; scroll-padding-inline: 8px; }
  .asset-category-tabs button { min-height: 48px; padding-inline: 10px; }
  .asset-commandbar { display: grid; min-height: 0; grid-template-columns: minmax(0, 1fr) auto auto; gap: 7px; padding: 8px 10px; }
  .asset-search { width: 100%; min-width: 0; max-width: none; grid-column: 1 / -1; }
  .asset-commandbar > .asset-action { min-width: 0; padding-inline: 9px; }
  .asset-batch-dock { flex-wrap: wrap; padding-inline: 10px; }
  .asset-batch-dock > span { min-width: 100%; }
  .asset-batch-dock button { min-width: 0; flex: 1 1 0; justify-content: center; padding-inline: 7px; }
  .asset-batch-dock button small { display: none; }
  .asset-results { overflow-x: hidden; overflow-y: auto; padding: 8px 10px calc(16px + env(safe-area-inset-bottom)); overscroll-behavior-y: contain; touch-action: pan-y; -webkit-overflow-scrolling: touch; }
  .asset-results::-webkit-scrollbar { width: 6px; }
  .asset-results::-webkit-scrollbar-thumb { border: 1px solid transparent; background: rgb(84 99 103 / 38%); background-clip: padding-box; }
  .asset-results[data-view='list'], .asset-results[data-view='grid'] { display: grid; grid-template-columns: 1fr; grid-auto-rows: max-content; align-content: start; gap: 10px; }
  .asset-results[data-view='list'] .asset-family { overflow: hidden; border-radius: 8px; background: #fff; box-shadow: 0 0 0 1px rgb(21 31 36 / 8%); }
  .asset-results[data-view='list'] .asset-record { position: relative; min-height: 154px; grid-template-columns: 88px minmax(0, 1fr); grid-template-rows: auto minmax(42px, auto) 44px; align-items: start; gap: 7px 10px; padding: 10px; }
  .asset-results[data-view='list'] .asset-record__expand { position: static; z-index: auto; width: 40px; height: 40px; grid-column: 1; grid-row: 3; align-self: center; justify-self: start; margin: 0; background: transparent; box-shadow: none; }
  .asset-results[data-view='list'] .asset-record__expand--empty { display: block; }
  .asset-results[data-view='list'] .asset-check { position: relative; z-index: auto; grid-column: 1; grid-row: 3; align-self: center; justify-self: end; margin: 0; background: transparent; box-shadow: none; }
  .asset-results[data-view='list'] .asset-record__preview { width: 88px; height: 88px; grid-column: 1; grid-row: 1 / 3; }
  .asset-results[data-view='list'] .asset-record__identity { grid-column: 2; grid-row: 1; align-self: center; }
  .asset-results[data-view='list'] .asset-record__prompt { grid-column: 2; grid-row: 2; align-self: start; }
  .asset-results[data-view='list'] .asset-record__description { display: none; }
  .asset-results[data-view='list'] .asset-record__actions { min-width: 0; grid-column: 2; grid-row: 3; flex-wrap: nowrap; justify-content: flex-start; border-top: 1px solid rgb(21 31 36 / 7%); padding-top: 3px; }
  .asset-results[data-view='list'] .asset-record__actions button { min-width: 40px; padding-inline: 6px; }
  .asset-results[data-view='list'] .asset-record__actions .danger { margin-left: auto; }
  .asset-results[data-view='list'] .asset-record--child { margin-left: 0; }
  .asset-derivatives { margin: 0; border-radius: 0; box-shadow: inset 0 1px rgb(21 31 36 / 7%); }
  .asset-derivatives > header { padding: 0 10px; }
  .asset-editor-drawer { position: fixed; z-index: 220; inset: 0; width: auto; height: auto; max-width: none; max-height: none; grid-template-rows: auto minmax(0, 1fr) auto; overflow: hidden; box-shadow: none; }
  .asset-editor-drawer > header { min-height: 64px; padding: calc(8px + env(safe-area-inset-top)) 10px 8px 14px; }
  .asset-editor-drawer__scroll { min-height: 0; overflow-x: hidden; overflow-y: auto; padding: 12px 12px 28px; overscroll-behavior-y: contain; touch-action: pan-y; -webkit-overflow-scrolling: touch; }
  .asset-editor-drawer__scroll::-webkit-scrollbar { width: 6px; }
  .asset-editor-drawer__scroll::-webkit-scrollbar-thumb { border: 1px solid transparent; background: rgb(84 99 103 / 38%); background-clip: padding-box; }
  .asset-editor-drawer > footer { position: relative; z-index: 1; min-height: 64px; flex-wrap: nowrap; padding: 8px 10px calc(8px + env(safe-area-inset-bottom)); box-shadow: 0 -1px 0 var(--studio-line); }
  .asset-editor-drawer > footer > button { min-width: 0; flex: 1 1 0; padding-inline: 7px; }
  .asset-hero-preview > div { aspect-ratio: 16 / 10; }
  .asset-detail-form__types { grid-template-columns: repeat(5, minmax(0, 1fr)); }
  .asset-detail-form__types button { padding-inline: 2px; }
}
@media (prefers-reduced-motion: reduce) {
  .asset-family { animation: none; }
  .asset-editor-drawer { transition-duration: 1ms; }
}
</style>
