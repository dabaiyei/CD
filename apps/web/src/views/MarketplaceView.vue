<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import {
  ArrowDownToLine,
  BadgeCheck,
  BookOpenText,
  Bot,
  Box,
  Check,
  Clipboard,
  Clock3,
  Code2,
  Copy,
  FileText,
  Film,
  Image,
  Images,
  Layers3,
  LoaderCircle,
  PackageOpen,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Share2,
  Sparkles,
  Store,
  Trash2,
  UploadCloud,
  UserRound,
  Volume2,
  X,
} from 'lucide-vue-next'

import BaseDialog from '@/components/BaseDialog.vue'
import { api } from '@/lib/api'
import { useTheme } from '@/lib/theme'
import { useToastStore } from '@/stores/toast'
import type {
  AssetItem,
  MarketplaceAcquisitionResult,
  MarketplaceListing,
  MarketplacePage,
  MarketplaceResourceType,
  UserSkill,
  UserSkillStage,
  UserTemplate,
} from '@/types'

type DialogKind = 'detail' | 'publish' | 'templates' | 'template-edit' | 'unpublish' | 'template-delete' | null
type PublicationSource = UserSkill | UserTemplate | AssetItem

const route = useRoute()
const toast = useToastStore()
const { theme } = useTheme()
const kind = computed<MarketplaceResourceType>(() => {
  const value = String(route.params.kind)
  return value === 'template' || value === 'material' ? value : 'skill'
})
const configs = {
  skill: {
    title: '技能广场',
    eyebrow: 'COMMUNITY SKILLS',
    description: '发现创作者公开的专业能力，让你的 AI 在需要的阶段掌握更准确的方法。',
    icon: Sparkles,
    action: '发布 Skill',
    empty: '还没有公开 Skill',
    coverDark: '/covers/skills-lab-v2.webp',
    coverLight: '/covers/skills-lab-light.webp',
  },
  template: {
    title: '模板广场',
    eyebrow: 'CREATIVE TEMPLATES',
    description: '收录可复用的创作结构、工作底稿与文本模板，复制后可以独立编辑。',
    icon: BookOpenText,
    action: '发布模板',
    empty: '还没有公开模板',
    coverDark: '/covers/default-project-campus-v2-wide.webp',
    coverLight: '/covers/studio-hero-light.webp',
  },
  material: {
    title: '素材广场',
    eyebrow: 'SHARED MATERIALS',
    description: '浏览社区共享的人物、场景、道具与声音素材，一键导入自己的全局资产库。',
    icon: Images,
    action: '发布素材',
    empty: '还没有公开素材',
    coverDark: '/covers/asset-studio-v2.webp',
    coverLight: '/covers/asset-studio-light.webp',
  },
} as const
const config = computed(() => configs[kind.value])
const heroCover = computed(() => theme.value === 'light' ? config.value.coverLight : config.value.coverDark)

const items = ref<MarketplaceListing[]>([])
const categories = ref<string[]>([])
const total = ref(0)
const loading = ref(true)
const search = ref('')
const category = ref('')
const sort = ref<'newest' | 'popular'>('newest')
const dialog = ref<DialogKind>(null)
const selectedListing = ref<MarketplaceListing | null>(null)
const sources = ref<PublicationSource[]>([])
const sourcesLoading = ref(false)
const selectedSourceId = ref('')
const publishCategory = ref('')
const publishTags = ref('')
const publishing = ref(false)
const acquiringId = ref<string | null>(null)
const templates = ref<UserTemplate[]>([])
const templatesLoading = ref(false)
const savingTemplate = ref(false)
const templateTarget = ref<UserTemplate | null>(null)
let searchTimer: ReturnType<typeof setTimeout> | null = null

const templateForm = reactive({ id: '', name: '', description: '', category: '通用', content: '' })
const assetTypeMeta = {
  character: { label: '人物', icon: UserRound },
  scene: { label: '场景', icon: Film },
  prop: { label: '道具', icon: Box },
  material: { label: '素材', icon: Image },
  audio: { label: '音频', icon: Volume2 },
} as const
const stageLabels: Record<UserSkillStage, string> = {
  script_generation: '剧本生成',
  script_review: '剧本审核',
  asset_extraction: '资产提取',
  asset_prompt_generation: '资产提示词',
  storyboard_generation: '分镜生成',
  storyboard_review: '分镜审核',
  video_generation: '视频生成',
}

const payloadStages = (listing: MarketplaceListing): UserSkillStage[] => (
  Array.isArray(listing.payload.trigger_stages)
    ? listing.payload.trigger_stages.filter((value): value is UserSkillStage => typeof value === 'string' && value in stageLabels)
    : []
)
const payloadText = (listing: MarketplaceListing, key: string): string => {
  const value = listing.payload[key]
  return typeof value === 'string' ? value : ''
}
const materialMeta = (listing: MarketplaceListing) => {
  const type = payloadText(listing, 'asset_type') as keyof typeof assetTypeMeta
  return assetTypeMeta[type] ?? assetTypeMeta.material
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', year: 'numeric' }).format(new Date(value))
}

async function loadListings(): Promise<void> {
  loading.value = true
  try {
    const params = new URLSearchParams({ sort: sort.value, limit: '60' })
    if (search.value.trim()) params.set('search', search.value.trim())
    if (category.value) params.set('category', category.value)
    const page = await api<MarketplacePage>(`/marketplace/${kind.value}?${params.toString()}`)
    items.value = page.items
    total.value = page.total
    categories.value = page.categories
    if (category.value && !page.categories.includes(category.value)) category.value = ''
  } catch (error) {
    toast.show('广场内容加载失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    loading.value = false
  }
}

function scheduleSearch(): void {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => void loadListings(), 260)
}

watch(search, scheduleSearch)
watch([category, sort], () => void loadListings())
watch(kind, () => {
  search.value = ''
  category.value = ''
  sort.value = 'newest'
  dialog.value = null
  void loadListings()
}, { immediate: true })
onBeforeUnmount(() => { if (searchTimer) clearTimeout(searchTimer) })

async function loadSources(): Promise<void> {
  sourcesLoading.value = true
  try {
    if (kind.value === 'skill') sources.value = await api<UserSkill[]>('/user-skills')
    else if (kind.value === 'template') {
      templates.value = await api<UserTemplate[]>('/user-templates')
      sources.value = templates.value
    } else sources.value = await api<AssetItem[]>('/marketplace/material-sources')
  } finally {
    sourcesLoading.value = false
  }
}

async function openPublish(source?: PublicationSource): Promise<void> {
  await loadSources()
  if (!sources.value.length) {
    if (kind.value === 'template') {
      openTemplateEditor()
      toast.show('先创建一个模板', { message: '保存后即可发布到模板广场', tone: 'info' })
    } else {
      toast.show(kind.value === 'skill' ? '还没有可发布的个人 Skill' : '还没有可发布的个人全局资产', { tone: 'info' })
    }
    return
  }
  const initial = source ?? sources.value[0]
  if (!initial) return
  selectedSourceId.value = initial.id
  publishCategory.value = sourceCategory(initial)
  publishTags.value = ''
  dialog.value = 'publish'
}

function sourceCategory(source: PublicationSource): string {
  if ('category' in source) return source.category
  if ('asset_type' in source) return assetTypeMeta[source.asset_type]?.label ?? '素材'
  return '创作技能'
}

function sourceAssetMeta(source: PublicationSource) {
  return 'asset_type' in source
    ? assetTypeMeta[source.asset_type] ?? assetTypeMeta.material
    : null
}

function sourceDescription(source: PublicationSource): string {
  return source.description || ('asset_type' in source ? source.generation_prompt : '') || '暂无说明'
}

function selectSource(source: PublicationSource): void {
  selectedSourceId.value = source.id
  publishCategory.value = sourceCategory(source)
}

async function publishSelected(): Promise<void> {
  if (!selectedSourceId.value) return
  publishing.value = true
  try {
    const segment = kind.value === 'skill' ? 'skills' : kind.value === 'template' ? 'templates' : 'materials'
    await api(`/marketplace/${segment}/${selectedSourceId.value}/publish`, {
      method: 'POST',
      body: JSON.stringify({
        category: publishCategory.value,
        tags: publishTags.value.split(/[,，]/).map((item) => item.trim()).filter(Boolean),
      }),
    })
    toast.show(`${config.value.title.replace('广场', '')}已发布`, { message: '其他用户现在可以查看并复制这个版本', tone: 'success' })
    dialog.value = null
    await loadListings()
  } catch (error) {
    toast.show('发布失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    publishing.value = false
  }
}

async function acquire(listing: MarketplaceListing): Promise<void> {
  acquiringId.value = listing.id
  try {
    const result = await api<MarketplaceAcquisitionResult>(`/marketplace/listings/${listing.id}/acquire`, { method: 'POST' })
    const destination = result.target_type === 'user_skill' ? '我的 Skills' : result.target_type === 'user_template' ? '我的模板' : '全局资产库'
    toast.show(listing.has_update ? '已同步广场最新版' : `已复制到${destination}`, {
      message: '副本归你所有，可以继续独立编辑',
      tone: 'success',
    })
    await loadListings()
    if (kind.value === 'template') await loadTemplates()
  } catch (error) {
    toast.show('复制失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    acquiringId.value = null
  }
}

async function unpublish(): Promise<void> {
  if (!selectedListing.value) return
  publishing.value = true
  try {
    await api(`/marketplace/listings/${selectedListing.value.id}`, { method: 'DELETE' })
    toast.show('资源已从广场下架', { message: '已复制到其他用户资源库的副本不会被删除', tone: 'success' })
    dialog.value = null
    await loadListings()
  } catch (error) {
    toast.show('下架失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    publishing.value = false
  }
}

async function copyTemplateContent(content: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(content)
    toast.show('模板正文已复制', { tone: 'success' })
  } catch {
    toast.show('复制失败', { message: '浏览器未授予剪贴板权限', tone: 'error' })
  }
}

async function loadTemplates(): Promise<void> {
  templatesLoading.value = true
  try { templates.value = await api<UserTemplate[]>('/user-templates') }
  catch (error) { toast.show('个人模板加载失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' }) }
  finally { templatesLoading.value = false }
}

async function openTemplates(): Promise<void> {
  await loadTemplates()
  dialog.value = 'templates'
}

function openTemplateEditor(template?: UserTemplate): void {
  Object.assign(templateForm, template ? {
    id: template.id, name: template.name, description: template.description, category: template.category, content: template.content,
  } : { id: '', name: '', description: '', category: '通用', content: '' })
  dialog.value = 'template-edit'
}

async function saveTemplate(): Promise<void> {
  savingTemplate.value = true
  try {
    const path = templateForm.id ? `/user-templates/${templateForm.id}` : '/user-templates'
    await api<UserTemplate>(path, {
      method: templateForm.id ? 'PATCH' : 'POST',
      body: JSON.stringify({
        name: templateForm.name,
        description: templateForm.description,
        category: templateForm.category,
        content: templateForm.content,
      }),
    })
    toast.show(templateForm.id ? '模板已更新' : '模板已创建', { tone: 'success' })
    await loadTemplates()
    dialog.value = 'templates'
  } catch (error) {
    toast.show('模板保存失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    savingTemplate.value = false
  }
}

async function deleteTemplate(): Promise<void> {
  if (!templateTarget.value) return
  savingTemplate.value = true
  try {
    await api(`/user-templates/${templateTarget.value.id}`, { method: 'DELETE' })
    toast.show('模板已删除', { tone: 'success' })
    templateTarget.value = null
    await loadTemplates()
    dialog.value = 'templates'
    await loadListings()
  } catch (error) {
    toast.show('模板删除失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    savingTemplate.value = false
  }
}
</script>

<template>
  <div class="marketplace-page page-stack">
    <section class="marketplace-hero" :style="{ '--marketplace-cover': `url(${heroCover})` }">
      <div class="marketplace-hero__veil" aria-hidden="true"></div>
      <div class="marketplace-hero__content t-stagger is-shown">
        <span class="marketplace-hero__eyebrow t-stagger-line t-stagger-line--1"><component :is="config.icon" :size="15" />{{ config.eyebrow }}</span>
        <h1 class="t-stagger-line t-stagger-line--2">{{ config.title }}</h1>
        <p class="t-stagger-line t-stagger-line--3">{{ config.description }}</p>
      </div>
      <div class="marketplace-hero__actions">
        <RouterLink v-if="kind === 'skill'" class="button marketplace-button--glass" to="/skills"><Bot :size="16" />我的 Skills</RouterLink>
        <button v-if="kind === 'template'" class="button marketplace-button--glass" type="button" @click="openTemplates"><BookOpenText :size="16" />我的模板</button>
        <button class="button marketplace-button--bright" type="button" @click="openPublish()"><UploadCloud :size="16" />{{ config.action }}</button>
      </div>
      <span class="marketplace-hero__count"><strong class="tabular-nums">{{ total }}</strong> PUBLIC RESOURCES</span>
    </section>

    <main class="marketplace-workspace">
      <header class="marketplace-toolbar">
        <label class="marketplace-search"><Search :size="17" /><input v-model="search" type="search" :placeholder="`搜索${config.title.replace('广场', '')}、分类或说明`" /><button v-if="search" type="button" title="清空搜索" @click="search = ''"><X :size="15" /></button></label>
        <div class="marketplace-sort" aria-label="内容排序"><button type="button" :aria-pressed="sort === 'newest'" @click="sort = 'newest'"><Clock3 :size="14" />最新</button><button type="button" :aria-pressed="sort === 'popular'" @click="sort = 'popular'"><Store :size="14" />热门</button></div>
        <button class="icon-button" type="button" title="刷新广场" :disabled="loading" @click="loadListings"><RefreshCw :class="{ spin: loading }" :size="17" /></button>
      </header>

      <nav v-if="categories.length" class="marketplace-categories" aria-label="广场分类">
        <button type="button" :aria-pressed="!category" @click="category = ''">全部</button>
        <button v-for="item in categories" :key="item" type="button" :aria-pressed="category === item" @click="category = item">{{ item }}</button>
      </nav>

      <section v-if="loading" class="marketplace-grid" aria-label="正在加载">
        <article v-for="index in 6" :key="index" class="marketplace-skeleton"><i></i><i></i><i></i><i></i></article>
      </section>
      <section v-else-if="items.length" class="marketplace-grid" :aria-label="config.title">
        <article v-for="(listing, index) in items" :key="listing.id" v-motion="{ preset: 'card', index }" class="marketplace-card">
          <button class="marketplace-card__visual" type="button" :style="{ '--card-cover': `url(${listing.cover_url || heroCover})` }" @click="selectedListing = listing; dialog = 'detail'">
            <span class="marketplace-card__shade"></span>
            <span class="marketplace-card__kind"><component :is="kind === 'material' ? materialMeta(listing).icon : config.icon" :size="14" />{{ kind === 'material' ? materialMeta(listing).label : listing.category }}</span>
            <span v-if="listing.has_update" class="marketplace-card__update"><RefreshCw :size="12" />有新版</span>
            <span v-else-if="listing.acquired" class="marketplace-card__owned"><Check :size="12" />已复制</span>
            <strong>{{ listing.title }}</strong>
          </button>
          <div class="marketplace-card__body">
            <p>{{ listing.description || '作者暂未填写说明。' }}</p>
            <div v-if="kind === 'skill'" class="marketplace-card__stages"><span v-for="stage in payloadStages(listing).slice(0, 3)" :key="stage">{{ stageLabels[stage] }}</span></div>
            <div v-else class="marketplace-card__tags"><span v-for="tag in listing.tags.slice(0, 3)" :key="tag"># {{ tag }}</span><span v-if="!listing.tags.length"># {{ listing.category }}</span></div>
          </div>
          <footer>
            <button class="marketplace-author" type="button" @click="selectedListing = listing; dialog = 'detail'"><span><img v-if="listing.publisher_avatar_url" :src="listing.publisher_avatar_url" alt="" /><UserRound v-else :size="14" /></span><span><strong>{{ listing.publisher_name }}</strong><small>{{ formatDate(listing.updated_at) }} · <ArrowDownToLine :size="10" />{{ listing.download_count }}</small></span></button>
            <button v-if="listing.owned_by_me" class="marketplace-card__manage" type="button" title="下架资源" @click="selectedListing = listing; dialog = 'unpublish'"><Trash2 :size="16" /></button>
            <button v-else class="marketplace-card__acquire" type="button" :disabled="acquiringId === listing.id || (listing.acquired && !listing.has_update)" @click="acquire(listing)"><LoaderCircle v-if="acquiringId === listing.id" class="spin" :size="15" /><RefreshCw v-else-if="listing.has_update" :size="15" /><Check v-else-if="listing.acquired" :size="15" /><Copy v-else :size="15" />{{ listing.has_update ? '同步' : listing.acquired ? '已拥有' : '一键复制' }}</button>
          </footer>
        </article>
      </section>
      <section v-else class="marketplace-empty"><span><component :is="config.icon" :size="28" /></span><h2>{{ config.empty }}</h2><p>成为第一个向社区分享这类资源的人。</p><button class="button button--primary" type="button" @click="openPublish()"><Share2 :size="16" />{{ config.action }}</button></section>
    </main>

    <BaseDialog :open="dialog === 'detail'" :title="selectedListing?.title || config.title" wide @update:open="!$event && (dialog = null)">
      <article v-if="selectedListing" class="marketplace-detail">
        <div class="marketplace-detail__media" :style="{ '--detail-cover': `url(${selectedListing.cover_url || heroCover})` }"><span><component :is="kind === 'material' ? materialMeta(selectedListing).icon : config.icon" :size="24" /></span></div>
        <div class="marketplace-detail__content">
          <div class="marketplace-detail__meta"><span>{{ selectedListing.category }}</span><span class="tabular-nums">v{{ selectedListing.version }}</span><span><ArrowDownToLine :size="12" />{{ selectedListing.download_count }} 次复制</span></div>
          <p>{{ selectedListing.description || '作者暂未填写说明。' }}</p>
          <div class="marketplace-detail__tags"><span v-for="tag in selectedListing.tags" :key="tag"># {{ tag }}</span></div>
          <section v-if="kind === 'skill'" class="marketplace-detail__block"><header><Sparkles :size="15" />调用阶段</header><div class="marketplace-detail__stages"><span v-for="stage in payloadStages(selectedListing)" :key="stage"><Check :size="12" />{{ stageLabels[stage] }}</span></div><pre>{{ payloadText(selectedListing, 'description') }}</pre></section>
          <section v-else-if="kind === 'template'" class="marketplace-detail__block"><header><FileText :size="15" />模板正文<button type="button" @click="copyTemplateContent(payloadText(selectedListing, 'content'))"><Clipboard :size="14" />复制正文</button></header><pre>{{ payloadText(selectedListing, 'content') }}</pre></section>
          <section v-else class="marketplace-detail__block"><header><Image :size="15" />素材提示词</header><pre>{{ payloadText(selectedListing, 'generation_prompt') || '该素材未提供生成提示词。' }}</pre></section>
          <div class="marketplace-detail__publisher"><span><img v-if="selectedListing.publisher_avatar_url" :src="selectedListing.publisher_avatar_url" alt="" /><UserRound v-else :size="17" /></span><div><small>发布者</small><strong>{{ selectedListing.publisher_name }}</strong></div><BadgeCheck :size="17" /></div>
        </div>
      </article>
      <template #footer><button class="button button--ghost" type="button" @click="dialog = null">关闭</button><button v-if="selectedListing && !selectedListing.owned_by_me" class="button button--primary" type="button" :disabled="acquiringId === selectedListing.id || (selectedListing.acquired && !selectedListing.has_update)" @click="acquire(selectedListing)"><LoaderCircle v-if="acquiringId === selectedListing.id" class="spin" :size="16" /><RefreshCw v-else-if="selectedListing.has_update" :size="16" /><Copy v-else :size="16" />{{ selectedListing.has_update ? '同步最新版' : selectedListing.acquired ? '已经拥有' : '复制到我的资源库' }}</button></template>
    </BaseDialog>

    <BaseDialog :open="dialog === 'publish'" :title="config.action" description="选择一个自己的资源并发布当前版本快照" wide @update:open="!$event && !publishing && (dialog = null)">
      <form id="marketplace-publish-form" class="marketplace-publish" @submit.prevent="publishSelected">
        <div v-if="sourcesLoading" class="marketplace-dialog-loading"><LoaderCircle class="spin" :size="22" />正在读取可发布资源</div>
        <div v-else class="marketplace-source-grid">
          <button v-for="source in sources" :key="source.id" type="button" :aria-pressed="selectedSourceId === source.id" @click="selectSource(source)"><span><img v-if="'media_url' in source && source.media_url" :src="source.media_url" alt="" /><component :is="sourceAssetMeta(source)?.icon ?? (kind === 'skill' ? Sparkles : FileText)" v-else :size="19" /></span><div><strong>{{ source.name }}</strong><p>{{ sourceDescription(source) }}</p></div><Check :size="17" /></button>
        </div>
        <div class="marketplace-publish__fields"><label><span>广场分类</span><div><Layers3 :size="16" /><input v-model.trim="publishCategory" required maxlength="80" placeholder="例如：动作设计" /></div></label><label><span>标签 <small>用逗号分隔</small></span><div><Code2 :size="16" /><input v-model.trim="publishTags" maxlength="240" placeholder="打斗，运镜，节奏" /></div></label></div>
        <aside><Share2 :size="16" /><span>发布的是当前版本快照；修改私人源内容后，需要再次发布才会生成广场新版。</span></aside>
      </form>
      <template #footer><button class="button button--ghost" type="button" :disabled="publishing" @click="dialog = null">取消</button><button class="button button--primary" type="submit" form="marketplace-publish-form" :disabled="publishing || !selectedSourceId"><LoaderCircle v-if="publishing" class="spin" :size="16" /><UploadCloud v-else :size="16" />确认发布</button></template>
    </BaseDialog>

    <BaseDialog :open="dialog === 'templates'" title="我的模板" description="管理私人模板，或选择模板发布到广场" wide @update:open="!$event && (dialog = null)">
      <div class="personal-template-toolbar"><span><strong class="tabular-nums">{{ templates.length }}</strong> 个私人模板</span><button class="button button--primary" type="button" @click="openTemplateEditor()"><Plus :size="16" />新建模板</button></div>
      <div v-if="templatesLoading" class="marketplace-dialog-loading"><LoaderCircle class="spin" :size="22" />正在读取模板</div>
      <div v-else-if="templates.length" class="personal-template-list"><article v-for="template in templates" :key="template.id"><span><FileText :size="19" /></span><div><strong>{{ template.name }}</strong><p>{{ template.description || template.content }}</p><small>{{ template.category }} · v{{ template.version }} · {{ formatDate(template.updated_at) }}</small></div><div><button type="button" title="复制正文" @click="copyTemplateContent(template.content)"><Copy :size="15" /></button><button type="button" title="编辑模板" @click="openTemplateEditor(template)"><Pencil :size="15" /></button><button type="button" title="发布模板" @click="openPublish(template)"><Share2 :size="15" /></button><button class="danger" type="button" title="删除模板" @click="templateTarget = template; dialog = 'template-delete'"><Trash2 :size="15" /></button></div></article></div>
      <div v-else class="marketplace-empty marketplace-empty--dialog"><PackageOpen :size="28" /><h2>还没有私人模板</h2><button class="button button--primary" type="button" @click="openTemplateEditor()"><Plus :size="16" />创建模板</button></div>
      <template #footer><button class="button button--ghost" type="button" @click="dialog = null">关闭</button></template>
    </BaseDialog>

    <BaseDialog :open="dialog === 'template-edit'" :title="templateForm.id ? '编辑模板' : '新建模板'" wide @update:open="!$event && !savingTemplate && (dialog = null)">
      <form id="personal-template-form" class="personal-template-form" @submit.prevent="saveTemplate"><div class="personal-template-form__grid"><label><span>模板名称</span><div><BookOpenText :size="16" /><input v-model.trim="templateForm.name" required maxlength="120" placeholder="例如：悬疑开场结构" /></div></label><label><span>分类</span><div><Layers3 :size="16" /><input v-model.trim="templateForm.category" required maxlength="80" placeholder="剧本结构" /></div></label></div><label><span>模板说明</span><div><FileText :size="16" /><textarea v-model.trim="templateForm.description" maxlength="2000" rows="3" placeholder="说明适用场景和使用目标"></textarea></div></label><label><span>模板正文</span><div><Code2 :size="16" /><textarea v-model="templateForm.content" required maxlength="200000" rows="12" placeholder="输入可复用的模板正文、结构或工作底稿"></textarea></div></label></form>
      <template #footer><button class="button button--ghost" type="button" :disabled="savingTemplate" @click="dialog = null">取消</button><button class="button button--primary" type="submit" form="personal-template-form" :disabled="savingTemplate"><LoaderCircle v-if="savingTemplate" class="spin" :size="16" /><Check v-else :size="16" />保存模板</button></template>
    </BaseDialog>

    <BaseDialog :open="dialog === 'unpublish'" title="下架广场资源" @update:open="!$event && !publishing && (dialog = null)"><div class="marketplace-confirm"><span><Trash2 :size="22" /></span><div><strong>下架“{{ selectedListing?.title }}”</strong><p>广场中将不再展示该资源，其他用户已有的独立副本不会被删除。</p></div></div><template #footer><button class="button button--ghost" type="button" :disabled="publishing" @click="dialog = null">取消</button><button class="button button--danger" type="button" :disabled="publishing" @click="unpublish"><LoaderCircle v-if="publishing" class="spin" :size="16" /><Trash2 v-else :size="16" />确认下架</button></template></BaseDialog>
    <BaseDialog :open="dialog === 'template-delete'" title="删除私人模板" @update:open="!$event && !savingTemplate && (dialog = 'templates')"><div class="marketplace-confirm"><span><Trash2 :size="22" /></span><div><strong>删除“{{ templateTarget?.name }}”</strong><p>如果它已发布到广场，对应公开版本也会同时下架。</p></div></div><template #footer><button class="button button--ghost" type="button" :disabled="savingTemplate" @click="dialog = 'templates'">取消</button><button class="button button--danger" type="button" :disabled="savingTemplate" @click="deleteTemplate"><LoaderCircle v-if="savingTemplate" class="spin" :size="16" /><Trash2 v-else :size="16" />确认删除</button></template></BaseDialog>
  </div>
</template>

<style scoped>
.marketplace-page { width: min(100%, 1640px); gap: 0; padding: 0 clamp(14px, 2vw, 28px) 42px; color: var(--ink); -webkit-font-smoothing: antialiased; }
.marketplace-hero { position: relative; display: grid; min-height: 228px; grid-template-columns: minmax(0, 1fr) auto; align-items: center; overflow: hidden; padding: 34px clamp(24px, 4vw, 58px); border-radius: 8px; color: #fff; background: #17211d var(--marketplace-cover) center 42% / cover no-repeat; box-shadow: 0 24px 56px rgb(6 16 11 / 20%); isolation: isolate; }
.marketplace-hero__veil { position: absolute; z-index: -1; inset: 0; background: linear-gradient(90deg, rgb(6 13 10 / 80%) 0%, rgb(7 14 11 / 53%) 58%, rgb(7 13 10 / 25%) 100%); }
.marketplace-hero__content { max-width: 680px; }
.marketplace-hero__eyebrow { display: inline-flex; align-items: center; gap: 7px; color: rgb(211 249 225 / 76%); font-size: 9px; font-weight: 760; }
.marketplace-hero h1 { margin: 11px 0 8px; font-size: 38px; line-height: 1.1; letter-spacing: 0; text-wrap: balance; }
.marketplace-hero p { max-width: 600px; margin: 0; color: rgb(246 251 248 / 68%); font-size: 12px; line-height: 1.75; text-wrap: pretty; }
.marketplace-hero__actions { display: flex; align-items: center; gap: 8px; }
.marketplace-hero .button { min-height: 42px; border: 0; border-radius: 7px; }
.marketplace-button--glass { color: #fff; background: rgb(255 255 255 / 11%); box-shadow: inset 0 0 0 1px rgb(255 255 255 / 12%); backdrop-filter: blur(14px); }
.marketplace-button--bright { color: #102019; background: #d2f5df; box-shadow: 0 12px 28px rgb(5 23 13 / 24%); }
.marketplace-hero__count { position: absolute; right: 18px; bottom: 13px; color: rgb(255 255 255 / 48%); font-size: 8px; }
.marketplace-hero__count strong { color: #fff; font-size: 11px; font-variant-numeric: tabular-nums; }
.marketplace-workspace { display: grid; gap: 14px; padding-top: 20px; }
.marketplace-toolbar { display: grid; grid-template-columns: minmax(260px, 1fr) auto 40px; align-items: center; gap: 9px; }
.marketplace-search { display: flex; min-height: 42px; align-items: center; gap: 9px; padding: 0 11px; border-radius: 7px; color: var(--ink-tertiary); background: var(--surface); box-shadow: var(--shadow-border); transition-property: box-shadow, background-color; transition-duration: var(--duration-fast); }
.marketplace-search:focus-within { color: var(--brand); background: var(--surface-strong); box-shadow: inset 0 0 0 1px var(--brand), 0 0 0 3px var(--brand-soft); }
.marketplace-search input { min-width: 0; flex: 1; border: 0; outline: 0; color: var(--ink); background: transparent; font-size: 11px; }
.marketplace-search button { display: inline-flex; width: 40px; height: 40px; align-items: center; justify-content: center; border: 0; color: var(--ink-tertiary); background: transparent; }
.marketplace-sort { display: flex; padding: 3px; border-radius: 7px; background: var(--surface); box-shadow: var(--shadow-border); }
.marketplace-sort button { display: inline-flex; min-height: 34px; align-items: center; gap: 5px; padding: 0 11px; border: 0; border-radius: 5px; color: var(--ink-tertiary); background: transparent; font-size: 10px; font-weight: 650; transition-property: color, background-color, box-shadow; transition-duration: var(--duration-fast); }
.marketplace-sort button[aria-pressed='true'] { color: var(--ink); background: var(--surface-strong); box-shadow: 0 3px 10px rgb(0 0 0 / 7%); }
.marketplace-categories { display: flex; gap: 6px; overflow-x: auto; padding: 1px 0 3px; scrollbar-width: none; }
.marketplace-categories::-webkit-scrollbar { display: none; }
.marketplace-categories button { min-width: max-content; min-height: 34px; padding: 0 12px; border: 0; border-radius: 17px; color: var(--ink-secondary); background: var(--surface); box-shadow: var(--shadow-border); font-size: 10px; transition-property: color, background-color, scale; transition-duration: var(--duration-fast); }
.marketplace-categories button[aria-pressed='true'] { color: var(--brand-strong); background: var(--brand-soft); }
.marketplace-categories button:active { scale: .96; }
.marketplace-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(270px, 1fr)); gap: 13px; }
.marketplace-card { display: grid; min-width: 0; grid-template-rows: 146px minmax(92px, 1fr) 54px; overflow: hidden; border-radius: 8px; background: var(--surface); box-shadow: var(--shadow-border), 0 12px 30px rgb(0 0 0 / 6%); transition-property: transform, box-shadow; transition-duration: var(--duration-fast); }
.marketplace-card:hover { transform: translateY(-3px); box-shadow: var(--shadow-hover); }
.marketplace-card__visual { position: relative; display: flex; min-width: 0; align-items: flex-end; overflow: hidden; padding: 16px; border: 0; color: #fff; background: #19231f var(--card-cover) center / cover no-repeat; text-align: left; }
.marketplace-card__visual::after { position: absolute; inset: 0; outline: 1px solid rgb(255 255 255 / 10%); outline-offset: -1px; content: ''; pointer-events: none; }
.marketplace-card__shade { position: absolute; inset: 0; background: linear-gradient(180deg, rgb(5 10 8 / 5%), rgb(5 10 8 / 78%)); }
.marketplace-card__visual>strong { position: relative; z-index: 1; overflow: hidden; font-size: 17px; line-height: 1.3; text-overflow: ellipsis; white-space: nowrap; }
.marketplace-card__kind, .marketplace-card__update, .marketplace-card__owned { position: absolute; z-index: 1; top: 11px; display: inline-flex; min-height: 28px; align-items: center; gap: 5px; padding: 0 9px; border-radius: 14px; font-size: 9px; font-weight: 700; backdrop-filter: blur(12px); }
.marketplace-card__kind { left: 11px; background: rgb(8 14 11 / 48%); box-shadow: inset 0 0 0 1px rgb(255 255 255 / 12%); }
.marketplace-card__update, .marketplace-card__owned { right: 11px; color: #d5f7e2; background: rgb(15 74 42 / 58%); }
.marketplace-card__body { display: grid; align-content: start; gap: 10px; padding: 14px; }
.marketplace-card__body p { display: -webkit-box; overflow: hidden; margin: 0; color: var(--ink-secondary); font-size: 11px; line-height: 1.65; text-wrap: pretty; -webkit-box-orient: vertical; -webkit-line-clamp: 3; }
.marketplace-card__stages, .marketplace-card__tags, .marketplace-detail__tags, .marketplace-detail__stages { display: flex; flex-wrap: wrap; gap: 5px; }
.marketplace-card__stages span, .marketplace-card__tags span { color: var(--ink-tertiary); font-size: 8px; }
.marketplace-card__stages span { min-height: 23px; padding: 5px 7px 0; border-radius: 5px; color: var(--brand-strong); background: var(--brand-soft); }
.marketplace-card>footer { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 7px; padding: 7px 9px 7px 13px; box-shadow: inset 0 1px var(--line); }
.marketplace-author { display: grid; min-width: 0; min-height: 40px; grid-template-columns: 30px minmax(0, 1fr); align-items: center; gap: 8px; border: 0; color: var(--ink); background: transparent; text-align: left; }
.marketplace-author>span:first-child, .marketplace-detail__publisher>span { display: inline-flex; width: 30px; height: 30px; align-items: center; justify-content: center; overflow: hidden; border-radius: 50%; color: var(--ink-tertiary); background: var(--surface-strong); }
.marketplace-author img, .marketplace-detail__publisher img { width: 100%; height: 100%; object-fit: cover; outline: 1px solid rgb(0 0 0 / 10%); }
:global(:root[data-theme='dark']) .marketplace-author img, :global(:root[data-theme='dark']) .marketplace-detail__publisher img { outline-color: rgb(255 255 255 / 10%); }
.marketplace-author strong, .marketplace-author small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.marketplace-author strong { font-size: 9px; }
.marketplace-author small { display: flex; align-items: center; gap: 3px; margin-top: 2px; color: var(--ink-tertiary); font-size: 8px; }
.marketplace-card__acquire, .marketplace-card__manage { display: inline-flex; min-height: 40px; align-items: center; justify-content: center; gap: 5px; border: 0; border-radius: 6px; font-size: 9px; font-weight: 700; transition-property: color, background-color, scale; transition-duration: var(--duration-fast); }
.marketplace-card__acquire { min-width: 82px; padding: 0 10px; color: var(--brand-strong); background: var(--brand-soft); }
.marketplace-card__manage { width: 40px; color: var(--danger); background: var(--danger-soft); }
.marketplace-card__acquire:active:not(:disabled), .marketplace-card__manage:active { scale: .96; }
.marketplace-card__acquire:disabled { color: var(--ink-tertiary); background: var(--surface-strong); }
.marketplace-empty { display: grid; min-height: 300px; place-items: center; align-content: center; gap: 9px; color: var(--ink-tertiary); text-align: center; }
.marketplace-empty>span { display: inline-flex; width: 58px; height: 58px; align-items: center; justify-content: center; border-radius: 8px; color: var(--brand); background: var(--brand-soft); }
.marketplace-empty h2 { color: var(--ink); font-size: 17px; }
.marketplace-empty p { margin: 0 0 6px; font-size: 11px; }
.marketplace-skeleton { display: grid; height: 292px; gap: 11px; padding: 12px; border-radius: 8px; background: var(--surface); box-shadow: var(--shadow-border); }
.marketplace-skeleton i { border-radius: 6px; background: linear-gradient(90deg, var(--surface-strong), var(--surface), var(--surface-strong)); background-size: 200% 100%; animation: marketplace-shimmer 1.35s linear infinite; }
.marketplace-skeleton i:first-child { height: 130px; }.marketplace-skeleton i:nth-child(2) { width: 62%; height: 15px; }.marketplace-skeleton i:nth-child(3) { height: 48px; }.marketplace-skeleton i:last-child { width: 40%; height: 30px; justify-self: end; }
@keyframes marketplace-shimmer { to { background-position: -200% 0; } }
.marketplace-detail { display: grid; grid-template-columns: minmax(260px, .85fr) minmax(0, 1.15fr); gap: 20px; }
.marketplace-detail__media { display: grid; min-height: 430px; place-items: center; border-radius: 8px; background: #17211d var(--detail-cover) center / cover no-repeat; box-shadow: inset 0 0 0 1px rgb(255 255 255 / 10%); }
.marketplace-detail__media>span { display: inline-flex; width: 58px; height: 58px; align-items: center; justify-content: center; border-radius: 50%; color: #fff; background: rgb(7 14 11 / 48%); backdrop-filter: blur(14px); }
.marketplace-detail__content { display: grid; align-content: start; gap: 13px; min-width: 0; }
.marketplace-detail__meta { display: flex; flex-wrap: wrap; gap: 6px; }.marketplace-detail__meta span { display: inline-flex; min-height: 26px; align-items: center; gap: 4px; padding: 0 8px; border-radius: 5px; color: var(--ink-secondary); background: var(--surface-strong); font-size: 9px; }
.marketplace-detail__content>p { margin: 0; color: var(--ink-secondary); line-height: 1.75; text-wrap: pretty; }
.marketplace-detail__tags span { color: var(--brand-strong); font-size: 9px; }
.marketplace-detail__block { display: grid; min-width: 0; gap: 8px; }.marketplace-detail__block header { display: flex; min-height: 32px; align-items: center; gap: 6px; color: var(--ink-secondary); font-size: 10px; font-weight: 700; }.marketplace-detail__block header button { display: inline-flex; min-height: 30px; align-items: center; gap: 5px; margin-left: auto; padding: 0 9px; border: 0; border-radius: 5px; color: var(--brand-strong); background: var(--brand-soft); font-size: 9px; }.marketplace-detail__block pre { max-height: 280px; overflow: auto; margin: 0; padding: 13px; border-radius: 7px; color: var(--ink-secondary); background: var(--surface-strong); font: inherit; font-size: 10px; line-height: 1.7; white-space: pre-wrap; overflow-wrap: anywhere; }
.marketplace-detail__stages span { display: inline-flex; min-height: 26px; align-items: center; gap: 4px; padding: 0 8px; border-radius: 5px; color: var(--brand-strong); background: var(--brand-soft); font-size: 9px; }
.marketplace-detail__publisher { display: grid; grid-template-columns: 38px minmax(0, 1fr) 18px; align-items: center; gap: 9px; margin-top: 3px; padding: 10px; border-radius: 7px; background: var(--surface-strong); }.marketplace-detail__publisher>span { width: 38px; height: 38px; }.marketplace-detail__publisher small, .marketplace-detail__publisher strong { display: block; }.marketplace-detail__publisher small { color: var(--ink-tertiary); font-size: 8px; }.marketplace-detail__publisher strong { margin-top: 2px; font-size: 10px; }.marketplace-detail__publisher>svg { color: var(--brand); }
.marketplace-source-grid { display: grid; max-height: 390px; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; overflow-y: auto; padding: 1px; }.marketplace-source-grid>button { display: grid; min-height: 76px; grid-template-columns: 48px minmax(0, 1fr) 18px; align-items: center; gap: 10px; padding: 9px; border: 0; border-radius: 8px; color: var(--ink); background: var(--surface-strong); box-shadow: inset 0 0 0 1px var(--line); text-align: left; transition-property: box-shadow, background-color, scale; transition-duration: var(--duration-fast); }.marketplace-source-grid>button:active { scale: .96; }.marketplace-source-grid>button[aria-pressed='true'] { background: var(--brand-soft); box-shadow: inset 0 0 0 1px var(--brand); }.marketplace-source-grid>button>span { display: inline-flex; width: 48px; height: 48px; align-items: center; justify-content: center; overflow: hidden; border-radius: 6px; color: var(--brand); background: var(--surface); }.marketplace-source-grid img { width: 100%; height: 100%; object-fit: cover; }.marketplace-source-grid strong, .marketplace-source-grid p { display: block; overflow: hidden; }.marketplace-source-grid strong { font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }.marketplace-source-grid p { display: -webkit-box; margin: 3px 0 0; color: var(--ink-tertiary); font-size: 9px; line-height: 1.4; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }.marketplace-source-grid>button>svg { opacity: 0; scale: .25; filter: blur(4px); color: var(--brand); transition-property: opacity, scale, filter; transition-duration: 300ms; transition-timing-function: cubic-bezier(.2, 0, 0, 1); }.marketplace-source-grid>button[aria-pressed='true']>svg { opacity: 1; scale: 1; filter: blur(0); }
.marketplace-publish { display: grid; gap: 15px; }.marketplace-publish__fields, .personal-template-form__grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }.marketplace-publish__fields label, .personal-template-form label { display: grid; gap: 6px; color: var(--ink-secondary); font-size: 10px; font-weight: 650; }.marketplace-publish__fields label>span small { color: var(--ink-tertiary); font-weight: 400; }.marketplace-publish__fields label>div, .personal-template-form label>div { display: grid; min-height: 42px; grid-template-columns: 38px minmax(0, 1fr); align-items: start; overflow: hidden; border-radius: 7px; background: var(--surface-strong); box-shadow: inset 0 0 0 1px var(--line); }.marketplace-publish__fields svg, .personal-template-form label>div>svg { align-self: start; justify-self: center; margin-top: 13px; color: var(--ink-tertiary); }.marketplace-publish__fields input, .personal-template-form input, .personal-template-form textarea { width: 100%; border: 0; outline: 0; color: var(--ink); background: transparent; }.marketplace-publish__fields input, .personal-template-form input { height: 42px; }.marketplace-publish aside { display: flex; align-items: flex-start; gap: 8px; padding: 10px; border-radius: 7px; color: var(--ink-tertiary); background: var(--surface-strong); font-size: 9px; line-height: 1.6; }.marketplace-publish aside svg { flex: 0 0 auto; color: var(--brand); }
.marketplace-dialog-loading { display: flex; min-height: 180px; align-items: center; justify-content: center; gap: 8px; color: var(--ink-tertiary); }
.personal-template-toolbar { display: flex; align-items: center; justify-content: space-between; margin-bottom: 11px; color: var(--ink-tertiary); font-size: 10px; }.personal-template-toolbar strong { color: var(--ink); font-size: 15px; }.personal-template-list { display: grid; gap: 7px; }.personal-template-list article { display: grid; min-height: 70px; grid-template-columns: 40px minmax(0, 1fr) auto; align-items: center; gap: 10px; padding: 8px 9px; border-radius: 7px; background: var(--surface-strong); }.personal-template-list article>span { display: inline-flex; width: 40px; height: 40px; align-items: center; justify-content: center; border-radius: 6px; color: var(--brand); background: var(--brand-soft); }.personal-template-list strong, .personal-template-list p, .personal-template-list small { display: block; }.personal-template-list strong { font-size: 11px; }.personal-template-list p { overflow: hidden; margin: 3px 0; color: var(--ink-secondary); font-size: 9px; text-overflow: ellipsis; white-space: nowrap; }.personal-template-list small { color: var(--ink-tertiary); font-size: 8px; }.personal-template-list article>div:last-child { display: flex; gap: 2px; }.personal-template-list article>div:last-child button { display: inline-flex; width: 40px; height: 40px; align-items: center; justify-content: center; border: 0; border-radius: 6px; color: var(--ink-secondary); background: transparent; }.personal-template-list article>div:last-child button:hover { background: var(--surface); }.personal-template-list article>div:last-child button.danger { color: var(--danger); }
.personal-template-form { display: grid; gap: 13px; }.personal-template-form textarea { min-height: 74px; resize: vertical; padding: 11px 10px 11px 0; line-height: 1.6; }.personal-template-form label:last-child textarea { min-height: 260px; }
.marketplace-confirm { display: grid; grid-template-columns: 46px minmax(0, 1fr); align-items: center; gap: 12px; }.marketplace-confirm>span { display: inline-flex; width: 46px; height: 46px; align-items: center; justify-content: center; border-radius: 7px; color: var(--danger); background: var(--danger-soft); }.marketplace-confirm p { margin: 4px 0 0; color: var(--ink-tertiary); font-size: 10px; line-height: 1.6; text-wrap: pretty; }
@media (max-width: 900px) { .marketplace-hero { grid-template-columns: 1fr; gap: 20px; }.marketplace-hero__actions { justify-content: flex-start; }.marketplace-detail { grid-template-columns: 1fr; }.marketplace-detail__media { min-height: 260px; }.marketplace-source-grid { grid-template-columns: 1fr; } }
@media (max-width: 600px) { .marketplace-page { padding: 0 10px 86px; }.marketplace-hero { min-height: 250px; padding: 24px 18px; }.marketplace-hero h1 { font-size: 31px; }.marketplace-hero__actions { display: grid; grid-template-columns: 1fr 1fr; }.marketplace-hero__actions .button:only-child { grid-column: 1 / -1; }.marketplace-toolbar { grid-template-columns: minmax(0, 1fr) 40px; }.marketplace-sort { grid-column: 1 / -1; grid-row: 2; }.marketplace-sort button { flex: 1; justify-content: center; }.marketplace-grid { grid-template-columns: 1fr; }.marketplace-card { grid-template-rows: 154px minmax(86px, auto) 54px; }.marketplace-publish__fields, .personal-template-form__grid { grid-template-columns: 1fr; }.personal-template-list article { grid-template-columns: 36px minmax(0, 1fr); }.personal-template-list article>span { width: 36px; height: 36px; }.personal-template-list article>div:last-child { grid-column: 1 / -1; justify-content: flex-end; }.marketplace-detail__media { min-height: 210px; } }
@media (prefers-reduced-motion: reduce) { .marketplace-card, .marketplace-categories button, .marketplace-source-grid>button, .marketplace-source-grid>button>svg, .marketplace-card__acquire, .marketplace-card__manage { transition: none !important; }.marketplace-skeleton i { animation: none !important; } }
</style>
