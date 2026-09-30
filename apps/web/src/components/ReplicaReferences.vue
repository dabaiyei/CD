<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { Upload, Images, Sparkles, X } from 'lucide-vue-next'
import UiSelect from '@/components/UiSelect.vue'
import { api, apiUpload } from '@/lib/api'
import type { AITask } from '@/types'
import type { ReplicaReference } from '@/lib/replicaPlan'

type Model = { id: string; name: string; type: string; is_default: boolean; capabilities: Record<string, unknown> }
const props = defineProps<{ analysisId: string; models: Model[]; tasks: AITask[]; shots?: {start: number; end: number}[] }>()
const references = defineModel<ReplicaReference[]>({ required: true })
const emit = defineEmits<{ refresh: []; added: [] }>()
const tab = ref('')
const error = ref('')
const busy = ref(false)
const upload = ref<HTMLInputElement>()
const query = ref('')
const assets = ref<{ id: string; name: string; url: string; scope: string }[]>([])
const more = ref(false)
let searchVersion = 0
let disposed = false
const controller = new AbortController()
const imageModels = computed(() => props.models.filter(m => m.type === 'image'))
const modelId = ref(imageModels.value.find(m => m.is_default)?.id || imageModels.value[0]?.id || '')
const imageModel = computed(() => imageModels.value.find(m => m.id === modelId.value))
const resolution = ref('1K')
const ratio = ref('16:9')
const prompt = ref('')
const useReferences = ref(false)
const choices = (key: string, fallback: string[]) => ((imageModel.value?.capabilities[key] as string[]) || fallback).map(value => ({ value, label: value }))
const imageTasks = computed(() => props.tasks.filter(t => t.task_type === 'video_replica_image' && t.request_payload.analysis_id === props.analysisId))
watch(modelId, () => {
  resolution.value = choices('resolutions', ['1K', '2K'])[0]?.value || '1K'
  ratio.value = choices('aspect_ratios', ['16:9', '9:16', '1:1'])[0]?.value || '16:9'
}, { immediate: true })
function add(reference: ReplicaReference) {
  if (disposed) return
  if (references.value.some(r => r.kind === reference.kind && r.id === reference.id)) return
  if (references.value.length >= 8) { error.value = '最多选择 8 张，实际可用数量取决于视频模型'; return }
  references.value = [...references.value, { role: 'character', target: '', shot_indices: [], ...reference }]
  emit('added')
}
async function uploadFiles(event: Event) {
  const files = Array.from((event.target as HTMLInputElement).files || [])
  ;(event.target as HTMLInputElement).value = ''
  busy.value = true; error.value = ''
  try {
    for (const file of files.slice(0, Math.max(0, 8 - references.value.length))) {
      if (file.size > 100 * 1024 * 1024) throw new Error('单张图片不能超过 100MB')
      const body = new FormData(); body.set('file', file)
      const item = await apiUpload<{ id: string; name: string; media_url: string }>('/agent/attachments', body, controller.signal, () => {})
      add({ kind: 'attachment', id: item.id, name: item.name, url: item.media_url, purpose: '保持参考主体外观一致' })
    }
  } catch (cause) { if (!disposed) error.value = cause instanceof Error ? cause.message : '上传失败' }
  finally { busy.value = false }
}
async function searchAssets(append = false) {
  const version = ++searchVersion
  error.value = ''
  try {
    const rows = await api<typeof assets.value>(`/video-replicas/assets?search=${encodeURIComponent(query.value)}&offset=${append ? assets.value.length : 0}`)
    if (disposed || version !== searchVersion) return
    assets.value = append ? [...assets.value, ...rows] : rows; more.value = rows.length === 60
  } catch (cause) { if (!disposed && version === searchVersion) error.value = cause instanceof Error ? cause.message : '资产加载失败' }
}
function toggle(next: string) { tab.value = tab.value === next ? '' : next; if (tab.value === 'assets') void searchAssets() }
function setScope(index: number, shot: number, enabled: boolean) {
  references.value = references.value.map((ref, i) => i !== index ? ref : { ...ref,
    shot_indices: enabled ? [...new Set([...(ref.shot_indices || []), shot])].sort((a,b) => a-b) : (ref.shot_indices || []).filter(n => n !== shot),
  })
}
async function generate() {
  busy.value = true; error.value = ''
  try {
    const resolutions = choices('resolutions', ['1K', '2K'])
    const ratios = choices('aspect_ratios', ['16:9', '9:16', '1:1'])
    resolution.value = resolutions.some(r => r.value === resolution.value) ? resolution.value : resolutions[0]?.value || '1K'
    ratio.value = ratios.some(r => r.value === ratio.value) ? ratio.value : ratios[0]?.value || '16:9'
    await api(`/video-replicas/${props.analysisId}/images`, { method: 'POST', body: JSON.stringify({
      model_id: modelId.value, prompt: prompt.value, resolution: resolution.value, aspect_ratio: ratio.value,
      references: useReferences.value ? references.value.map(({ kind, id, purpose, role, target }) => ({ kind, id, purpose, role, target })) : [],
    }) })
    if (!disposed) emit('refresh')
  } catch (cause) { if (!disposed) error.value = cause instanceof Error ? cause.message : '生图提交失败' }
  finally { busy.value = false }
}
async function retry(task: AITask) {
  try { await api(`/tasks/${task.id}/retry`, { method: 'POST' }); emit('refresh') }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '重试失败' }
}
onBeforeUnmount(() => { disposed = true; controller.abort(); searchVersion++ })
</script>

<template>
  <section class="reference-editor">
    <h3>我的参考图</h3>
    <p>添加人物、服装或产品图片，填写它在视频中的用途。添加后默认关闭原片画面参考，避免原人物干扰；需要时可在下方重新开启。</p>
    <div class="reference-actions">
      <input ref="upload" type="file" accept="image/*" multiple hidden @change="uploadFiles" />
      <button type="button" :disabled="busy || references.length >= 8" @click="upload?.click()"><Upload :size="16" />{{ busy ? '处理中…' : '上传图片' }}</button>
      <button type="button" :aria-expanded="tab === 'assets'" @click="toggle('assets')"><Images :size="16" />资产库</button>
      <button type="button" :aria-expanded="tab === 'generate'" @click="toggle('generate')"><Sparkles :size="16" />AI 生图</button>
    </div>
    <p v-if="error" role="alert">{{ error }}</p>
    <div class="reference-selected">
      <article v-for="(item, index) in references" :key="`${item.kind}-${item.id}`">
        <img v-image-preview="item.url" :src="item.url" :alt="item.name" />
        <label><span>图片 {{ index + 1 }} · {{ item.name }}</span><textarea :value="item.purpose" rows="2" maxlength="500" placeholder="如：替换所有镜头中的女主，保持脸型、发型和服装" @input="references = references.map((r, i) => i === index ? { ...r, purpose: ($event.target as HTMLTextAreaElement).value } : r)" /></label>
        <button type="button" :aria-label="`移除${item.name}`" @click="references = references.filter((_, i) => i !== index)"><X :size="17" /></button>
        <div class="reference-binding">
          <label>参考用途<UiSelect :model-value="item.role || 'character'" @update:model-value="item.role = $event as ReplicaReference['role']" :options="[{value:'character',label:'替换人物'},{value:'scene',label:'替换场景'},{value:'style',label:'画风参考'},{value:'composition',label:'已完成的场景首帧'}]" /></label>
          <label v-if="!item.role || item.role === 'character'">替换原片哪个人物<input v-model="item.target" maxlength="120" placeholder="如：左侧白衣女子 / 男主" /></label>
          <details v-if="shots?.length"><summary>适用镜头 · {{ item.shot_indices?.length ? item.shot_indices.join('、') : '全部镜头' }}</summary><small>不勾选表示全部；勾选后仅应用到指定镜头。</small><div class="reference-scope"><label v-for="(shot, n) in shots" :key="n"><input type="checkbox" :checked="item.shot_indices?.includes(n+1) || false" @change="setScope(index, n+1, ($event.target as HTMLInputElement).checked)" />镜头 {{ n+1 }} · {{ shot.start.toFixed(1) }}–{{ shot.end.toFixed(1) }}s</label></div></details>
        </div>
      </article>
    </div>
    <div v-if="tab === 'assets'" class="reference-picker">
      <form @submit.prevent="searchAssets()"><input v-model="query" placeholder="搜索个人及项目资产" aria-label="搜索资产" /><button type="submit">搜索</button></form>
      <div class="reference-grid"><button v-for="asset in assets" :key="asset.id" type="button" @click="add({ kind: 'asset', id: asset.id, name: asset.name, url: asset.url, purpose: `使用${asset.name}作为参考主体，保持外观一致` })"><img :src="asset.url" :alt="asset.name" loading="lazy" /><span>{{ asset.name }}</span></button></div>
      <p v-if="!assets.length">未找到有图片的资产。</p><button v-if="more" @click="searchAssets(true)">加载更多</button>
    </div>
    <div v-if="tab === 'generate'" class="reference-generator">
      <label>图片模型<UiSelect v-model="modelId" :options="imageModels.map(m => ({ value: m.id, label: m.name }))" /></label>
      <div class="reference-actions"><label>分辨率<UiSelect v-model="resolution" :options="choices('resolutions', ['1K', '2K'])" /></label><label>比例<UiSelect v-model="ratio" :options="choices('aspect_ratios', ['16:9', '9:16', '1:1'])" /></label></div>
      <label>生图提示词<textarea v-model="prompt" rows="4" maxlength="6000" placeholder="描述要生成的角色、服装、场景或首帧构图；也可粘贴上方片段提示词。" /></label>
      <label class="reference-check"><input v-model="useReferences" type="checkbox" />将当前参考图传入图片模型（图生图）</label>
      <button type="button" :disabled="busy || !modelId || !prompt.trim()" @click="generate">生成参考图</button>
    </div>
    <div v-if="imageTasks.length" class="reference-grid reference-results"><article v-for="task in imageTasks" :key="task.id">
      <template v-if="task.status === 'succeeded' && task.result_payload?.media_url"><img v-image-preview="String(task.result_payload.media_url)" :src="String(task.result_payload.media_url)" alt="AI 参考图" loading="lazy" /><button @click="add({ kind: 'image_task', id: task.id, url: String(task.result_payload?.media_url), name: 'AI 参考图', purpose: '使用此图的主体与构图作为参考' })">加入参考</button></template>
      <template v-else><p>{{ task.error_message || task.latest_message || '等待生图' }}</p><button v-if="task.status === 'failed' || task.status === 'cancelled'" @click="retry(task)">重试生图</button></template>
    </article></div>
  </section>
</template>

<style scoped>
.reference-binding{grid-column:1/-1;min-width:0;width:100%;display:grid;gap:8px}.reference-binding label{min-width:0}.reference-binding summary{min-height:44px;align-content:center;cursor:pointer;overflow-wrap:anywhere}.reference-binding small{color:var(--ink-secondary);font-size:12px}.reference-scope{max-height:200px;overflow:auto;display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:4px}.reference-scope label{display:flex!important;flex-direction:row!important;align-items:center;gap:8px;min-height:44px;margin:0!important;font-size:12px}.reference-scope input{width:18px!important;height:18px;flex-shrink:0}
.reference-editor button{display:inline-flex;align-items:center;justify-content:center;gap:7px;min-height:44px;border:0;border-radius:12px;padding:10px 12px;background:var(--glass-inset);box-shadow:0 0 0 1px var(--glass-edge);color:var(--ink);font:inherit;cursor:pointer}.reference-editor button:disabled{opacity:.5;cursor:not-allowed}.reference-editor button:active{transform:scale(.96)}.reference-editor label{display:flex;flex-direction:column;gap:8px;font-size:13px;margin:12px 0}.reference-editor textarea,.reference-editor input:not([type=checkbox]){box-sizing:border-box;width:100%;min-width:0;padding:10px;border:1px solid var(--glass-edge);border-radius:10px;background:var(--glass-inset);color:var(--ink);font:inherit;line-height:1.6}.reference-editor textarea{resize:vertical}.reference-editor input[hidden]{display:none}.reference-selected label{margin:0!important}
.reference-editor{margin:24px 0;min-width:0}.reference-editor h3{font-size:15px}.reference-editor p{font-size:13px;line-height:1.7;color:var(--ink-secondary);overflow-wrap:anywhere}.reference-actions{display:flex;flex-wrap:wrap;gap:10px}.reference-actions>label{flex:1;min-width:120px}.reference-editor button{min-height:44px}.reference-selected article{display:grid;grid-template-columns:80px minmax(0,1fr) 44px;gap:10px;align-items:center;margin:14px 0}.reference-selected img{width:80px;height:80px;object-fit:cover;border-radius:12px;cursor:zoom-in}.reference-selected label{margin:0;font-size:12px;min-width:0}.reference-selected label span{overflow-wrap:anywhere}.reference-editor img{outline:1px solid rgba(0,0,0,.1);outline-offset:-1px}:global(.dark) .reference-editor img{outline-color:rgba(255,255,255,.1)}.reference-picker,.reference-generator{padding:14px;background:var(--glass-inset);border-radius:18px;margin-top:12px}.reference-picker form{display:flex;gap:8px}.reference-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:12px;margin-top:12px;max-height:340px;overflow-y:auto}.reference-grid>button{display:flex;flex-direction:column;min-width:0;padding:8px}.reference-grid img{width:100%;aspect-ratio:1;object-fit:cover;border-radius:10px}.reference-grid span{font-size:12px;overflow-wrap:anywhere}.reference-results article{min-width:0}.reference-results button{width:100%}.reference-editor .reference-check{display:flex;flex-direction:row;align-items:center;gap:8px;min-height:44px}.reference-check input{width:20px!important;height:20px}.reference-results img{cursor:zoom-in}@media(max-width:480px){.reference-selected article{grid-template-columns:60px minmax(0,1fr) 44px}.reference-selected img{width:60px;height:70px}.reference-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.reference-actions>button{flex:1}.reference-picker,.reference-generator{padding:10px}}
</style>
