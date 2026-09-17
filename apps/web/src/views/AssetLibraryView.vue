<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Boxes, LoaderCircle, RefreshCw } from 'lucide-vue-next'
import AssetLibraryWorkbench from '@/components/AssetLibraryWorkbench.vue'
import UiSelect from '@/components/UiSelect.vue'
import { api } from '@/lib/api'
import type { AssetItem, PricingRule, Project } from '@/types'

const projects = ref<Project[]>([])
const pricing = ref<PricingRule[]>([])
const projectId = ref('none')
const projectAssets = ref<AssetItem[]>([])
const globalAssets = ref<AssetItem[]>([])
const loading = ref(true)
const error = ref('')
let requestVersion = 0
const projectOptions = computed(() => [
  { value: 'none', label: '仅管理我的全局资产' },
  ...projects.value.map(project => ({ value: project.id, label: project.name })),
])
const selectedProject = computed(() => projects.value.find(project => project.id === projectId.value))

async function loadAssets() {
  const version = ++requestVersion
  loading.value = true
  error.value = ''
  try {
    const [personal, local] = await Promise.all([
      api<AssetItem[]>('/assets'),
      projectId.value === 'none' ? Promise.resolve<AssetItem[]>([]) : api<AssetItem[]>(`/projects/${projectId.value}/assets`),
    ])
    if (version !== requestVersion) return
    globalAssets.value = personal
    projectAssets.value = local
  } catch (cause) {
    if (version === requestVersion) error.value = cause instanceof Error ? cause.message : '资产加载失败'
  } finally {
    if (version === requestVersion) loading.value = false
  }
}

async function initialize() {
  loading.value = true
  error.value = ''
  try {
    const [rows, prices] = await Promise.all([api<Project[]>('/projects'), api<PricingRule[]>('/pricing')])
    projects.value = rows
    pricing.value = prices
    await loadAssets()
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '资产库加载失败'
    loading.value = false
  }
}

function updateAssets(local: AssetItem[], personal: AssetItem[]) {
  projectAssets.value = local
  globalAssets.value = personal
}

watch(projectId, loadAssets)
onMounted(initialize)
onBeforeUnmount(() => { requestVersion++ })
</script>

<template>
  <div class="asset-library-page">
    <header class="asset-library-page__header">
      <div><h1><Boxes :size="26" />我的资产库</h1><p>收藏、整理与复用你的创作素材。</p></div>
      <div class="asset-library-page__controls">
        <label><span>关联项目</span><UiSelect v-model="projectId" :options="projectOptions" /></label>
        <button class="icon-button" type="button" title="刷新资产" :disabled="loading" @click="initialize"><RefreshCw :size="18" /></button>
      </div>
    </header>
    <div v-if="error" class="asset-library-page__state" role="alert"><p>{{ error }}</p><button class="button button--secondary" @click="initialize">重新加载</button></div>
    <div v-else-if="loading" class="asset-library-page__state" role="status"><LoaderCircle class="spin" :size="25" /><span>正在读取资产…</span></div>
    <AssetLibraryWorkbench v-else :key="projectId" standalone
      :project-id="selectedProject?.id" :project-name="selectedProject?.name || '我的资产库'"
      :project-assets="projectAssets" :global-assets="globalAssets" :pricing="pricing"
      @assets-changed="updateAssets" />
  </div>
</template>

<style scoped>
.asset-library-page { display: flex; flex-direction: column; gap: 20px; min-width: 0; width: min(100%, 1520px); margin-inline: auto; padding: clamp(16px, 3vw, 40px); }
.asset-library-page__header { display: flex; align-items: center; justify-content: space-between; gap: 20px; flex-wrap: wrap; }
h1 { display: flex; align-items: center; gap: 10px; margin: 0; font-size: clamp(24px, 3vw, 32px); }
p { margin: 8px 0 0; color: var(--ink-secondary); }
.asset-library-page__controls { display: flex; align-items: end; gap: 10px; max-width: 100%; }
label { display: grid; gap: 7px; width: min(320px, 70vw); min-width: 0; }
label > span { color: var(--ink-secondary); font-size: 12px; }
.asset-library-page__state { display: grid; place-content: center; justify-items: center; gap: 14px; min-height: 300px; }
</style>
