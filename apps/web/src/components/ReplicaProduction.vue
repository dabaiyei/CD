<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Download, ExternalLink, Film, FolderOpen, Save, Sparkles, Upload, X } from 'lucide-vue-next'
import UiSelect from '@/components/UiSelect.vue'
import ReplicaCutEditor from '@/components/ReplicaCutEditor.vue'
import { api, apiBlob, apiUpload } from '@/lib/api'
import type { AITask } from '@/types'

type Production = { revision: string; files: Record<string, string>; assets: Record<string, {type:string; name?:string; duration?:number; url?:string}> }
const props = defineProps<{ taskId: string; models: { id:string; name:string; type:string; capabilities?:Record<string,unknown> }[] }>()
const emit = defineEmits<{ created: [task: AITask] }>()
const production = ref<Production | null>(null)
const busy = ref(false), error = ref(''), notice = ref(''), tab = ref('cut')
const cutDirty = ref(false), cutVersion = ref(0)
async function cutSaved() {
  await run(async () => {
    production.value = await api<Production>(`/video-replicas/${props.taskId}/production`)
    notice.value = '剪辑已保存，点击导出当前成片生成视频'
  })
}
const studio = ref<{id:string;url:string} | null>(null)
const model = ref(props.models.find(m => m.type === 'text')?.id || '')
const instruction = ref(''), craft = ref('picture'), currentFile = ref('TREATMENT.md')
const language = ref('zh')
const speechModel = ref(props.models.find(m=>m.type==='tts')?.id || '')
const speechText = ref(''), speechVoice = ref(''), speechDirection = ref('')
const voices = computed(() => {
  const values = props.models.find(m=>m.id===speechModel.value)?.capabilities?.voices
  return Array.isArray(values) ? values.map(v => typeof v === 'string' ? {value:v,label:v} : {value:String(v.id || v.value || ''),label:String(v.name || v.label || v.id || v.value || '')}).filter(v=>v.value) : []
})
watch(speechModel,()=>{speechVoice.value=voices.value[0]?.value || ''},{immediate:true})
const dirty = ref(false), uploadPercent = ref(0)
const labels: Record<string,string> = { 'BRIEF.md':'创作需求','ANALYSIS.md':'全片分析','TREATMENT.md':'改编方案','TIMELINE.md':'时间轴证据','PROGRESS.md':'制作进度','TRANSCRIPT.md':'成片对白与时间','FEEDBACK.json':'时间点反馈','composition.svml':'画面与时间轴','look.svs':'视觉样式','render.svrun':'导出配置' }
const crafts: Record<string,string[]> = { picture:['media-track','spatial'], titles:['typography-track','text','fonts-open','spatial'], captions:['caption-fine','script','whisperx','fonts-open'], audio:['audio-track','media-pipeline'], layout:['media-track','spatial','film'] }
const fileNames = computed(() => Object.keys(production.value?.files || {}))
let disposed = false
let uploadController: AbortController | undefined

async function run(action: () => Promise<void>) {
  if (busy.value) return
  busy.value = true; error.value = ''; notice.value = ''
  try { await action() } catch(cause) { if (!disposed) error.value = cause instanceof Error ? cause.message : '操作失败' }
  finally { if (!disposed) busy.value = false }
}
async function saveSources() {
  if (!production.value || !dirty.value) return
  production.value = await api<Production>(`/video-replicas/${props.taskId}/production`, { method:'PUT', body:JSON.stringify({revision:production.value.revision,files:production.value.files}) })
  dirty.value = false
  cutVersion.value++
}
async function saveStudio() {
  if (!studio.value) return
  production.value = await api<Production>(`/video-replicas/${props.taskId}/studio/${studio.value.id}/save`, {method:'POST'})
  dirty.value = false
  cutVersion.value++
}
async function openStudio() {
  await run(async () => {
    await saveSources()
    const opened = await api<{id:string;url:string}>(`/video-replicas/${props.taskId}/studio`, {method:'POST'})
    if (disposed) { await api(`/video-replicas/${props.taskId}/studio/${opened.id}`, {method:'DELETE'}); return }
    studio.value = opened
  })
}
async function closeStudio() {
  await run(async () => {
    await saveStudio()
    if (studio.value) await api(`/video-replicas/${props.taskId}/studio/${studio.value.id}`, {method:'DELETE'})
    studio.value = null; notice.value = '编辑已保存'
  })
}
async function submit(operation: 'edit' | 'export' | 'speech') {
  if (cutDirty.value) {error.value='请先保存或撤销剪辑修改';tab.value='cut';return}
  await run(async () => {
    if (studio.value) await saveStudio()
    else await saveSources()
    const task = await api<AITask>(`/video-replicas/${props.taskId}/production/${operation}`, { method:'POST', body:JSON.stringify({revision:production.value?.revision,instruction:instruction.value,text_model_id:model.value || null,components:crafts[craft.value],language:language.value,speech:operation==='speech'?{model_id:speechModel.value,text:speechText.value,voice:speechVoice.value,instructions:speechDirection.value}:null}) })
    emit('created',task)
  })
}
async function download() {
  await run(async () => {
    if (studio.value) await saveStudio()
    else await saveSources()
    const result = await apiBlob(`/video-replicas/${props.taskId}/production/archive`)
    const url = URL.createObjectURL(result.blob)
    const link = document.createElement('a'); link.href = url; link.download = result.filename || 'hypit-production.zip'; link.click()
    setTimeout(() => URL.revokeObjectURL(url), 30_000)
  })
}
async function upload(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]; input.value = ''
  if (!file) return
  if (file.size > 100*1024*1024) { error.value = '素材不能超过100MB'; return }
  await run(async () => {
    await saveSources()
    uploadController = new AbortController()
    const body = new FormData(); body.set('file', file)
    production.value = await apiUpload<Production>(`/video-replicas/${props.taskId}/production/assets/upload`,body,uploadController.signal,p => {uploadPercent.value = p})
    notice.value = '素材已加入工程，可以让 AI 编排到指定位置'
  })
}
async function validate() {
  await run(async () => {
    await saveSources()
    await api(`/video-replicas/${props.taskId}/production/check`, {method:'POST'})
    notice.value = '已通过 Hypit 原生工程校验'
  })
}
onMounted(() => run(async () => {
  const value = await api<Production>(`/video-replicas/${props.taskId}/production`)
  if (!disposed) production.value = value
}))
onBeforeUnmount(() => {
  disposed = true; uploadController?.abort()
  if (studio.value) {
    const taskId = props.taskId, id = studio.value.id
    // If persistence conflicts, retain the native session so reopening can recover it.
    void api(`/video-replicas/${taskId}/studio/${id}/save`, {method:'POST'})
      .then(()=>api(`/video-replicas/${taskId}/studio/${id}`,{method:'DELETE'})).catch(()=>{})
  }
})
</script>

<template>
  <section class="production-workbench" aria-label="Hypit 制作工程">
    <header><div><span class="production-eyebrow">VIDEO EDITOR · Powered by Hypit</span><h3>成片剪辑</h3><p>剪掉多余内容、调整顺序，再为作品添加字幕与声音。</p></div><button :disabled="busy || !production || cutDirty" @click="submit('export')"><Film :size="16" />导出当前成片</button></header>
    <p v-if="error" class="production-message error" role="alert">{{ error }}</p>
    <p v-if="notice" class="production-message" role="status">{{ notice }}</p>
    <p v-if="busy" class="production-message" role="status">正在处理，请稍候{{ uploadPercent > 0 && uploadPercent < 100 ? ` · 上传 ${uploadPercent}%` : '' }}</p>
    <nav aria-label="制作工具"><button v-for="item in [{id:'cut',text:'剪辑'},{id:'assistant',text:'字幕与后期'},{id:'assets',text:'音乐与素材'},{id:'studio',text:'高级编辑'},{id:'documents',text:'工程文件'}]" :key="item.id" :aria-pressed="tab===item.id" :disabled="cutDirty && item.id!=='cut'" @click="tab=item.id">{{ item.text }}</button></nav>
    <ReplicaCutEditor v-if="production" v-show="tab==='cut'" :key="cutVersion" :task-id="taskId" :locked="!!studio || busy" @dirty="cutDirty=$event" @saved="cutSaved" @advanced="tab='studio'" @assistant="craft=$event;tab='assistant'" />
    <div v-show="tab==='studio'" class="production-studio">
      <div v-if="!studio" class="production-launch"><Film :size="32" /><h4>打开原生 Hypit Studio</h4><p>在同一个工程中预览、调整时间轴、编辑组件参数和源文件，并留下时间点反馈。</p><button class="production-primary" :disabled="busy || !production" @click="openStudio"><ExternalLink :size="16" />打开制作编辑器</button></div>
      <template v-else><div class="production-tools"><button :disabled="busy" @click="run(async()=>{await saveStudio();notice='编辑器修改已保存'})"><Save :size="16" />同步编辑器修改</button><button :disabled="busy" @click="closeStudio"><X :size="16" />保存并关闭</button></div><iframe :src="studio.url" title="Hypit 原生制作编辑器" referrerpolicy="no-referrer" allow="fullscreen" /><p class="production-hint">参数和时间轴调整自动保存；源码编辑使用编辑器的保存按钮。移动端可横屏使用时间轴；编辑器闲置30分钟后关闭。</p></template>
    </div>
    <div v-if="tab==='assistant'" class="production-assistant">
      <label>想调整什么<UiSelect v-model="craft" :options="[{value:'picture',label:'画面 / B-roll / 转场'},{value:'titles',label:'标题与文字排版'},{value:'captions',label:'对白字幕与样式'},{value:'audio',label:'音乐与音效'},{value:'layout',label:'画幅与构图布局'}]" /></label>
      <label>修改要求<textarea v-model="instruction" rows="4" maxlength="4000" placeholder="例如：在第2秒出现标题，将刚上传的图片作为右侧画中画，在结尾淡出背景音乐。" /></label>
      <label>编辑模型<UiSelect v-model="model" :options="models.filter(m=>m.type==='text').map(m=>({value:m.id,label:m.name}))" /></label>
      <label v-if="craft==='captions'">成片对白语言<UiSelect v-model="language" :options="[{value:'zh',label:'中文'},{value:'en',label:'英语'}]" /></label>
      <p class="production-hint">只读取本次需要的组件说明。AI 修改通过原生校验后保存为可检查的工程，不重新调用生图或视频模型。</p>
      <button :disabled="busy || !production || !instruction.trim() || !model" @click="submit('edit')"><Sparkles :size="16" />生成工程修改</button>
    </div>
    <div v-if="tab==='assets' && production" class="production-materials">
      <label class="production-upload" :aria-disabled="busy || !!studio"><Upload :size="18" />导入图片、视频或音频 · 单份100MB<input type="file" accept="image/*,video/*,audio/*" :disabled="busy || !!studio" @change="upload" /></label><p v-if="studio" class="production-hint">请先保存并关闭编辑器，再导入新素材。</p>
      <div class="production-assets"><article v-for="(asset,name) in production.assets" :key="name"><FolderOpen :size="18" /><div><strong>{{ asset.name || name }}</strong><small>{{ asset.type }}{{ asset.duration ? ` · ${asset.duration.toFixed(1)} 秒` : '' }}</small><code>{{ name }}</code></div></article></div>
      <details><summary>AI 配音 · 使用现有模型平台</summary><div class="production-assistant"><label>语音模型<UiSelect v-model="speechModel" :options="models.filter(m=>m.type==='tts').map(m=>({value:m.id,label:m.name}))" placeholder="请选择已配置的配音模型" /></label><label>音色<UiSelect v-if="voices.length" v-model="speechVoice" :options="voices" /><input v-else v-model="speechVoice" placeholder="填写模型支持的音色 ID" maxlength="200" /></label><label>配音文本<textarea v-model="speechText" rows="4" maxlength="6000" /></label><label>表演与语气<textarea v-model="speechDirection" rows="2" maxlength="1500" placeholder="例如：从容叙述，重点词轻微加重" /></label><button :disabled="busy || !speechModel || !speechVoice.trim() || !speechText.trim()" @click="submit('speech')"><Sparkles :size="16" />生成配音素材</button><p class="production-hint">生成后在 AI 后期中把配音放入时间轴，可选择替换原声或作为独立旁白。</p></div></details>
    </div>
    <div v-if="tab==='documents' && production" class="production-documents">
      <button :disabled="busy" @click="download"><Download :size="16" />下载完整工程</button>
      <p class="production-hint">全片理解和创作决定保存在工程中。源文件在同一位置修改，导出时复用已有素材。</p>
      <UiSelect v-model="currentFile" :options="fileNames.map(name=>({value:name,label:labels[name] || name}))" />
      <textarea v-model="production.files[currentFile]" :disabled="!!studio" rows="14" spellcheck="false" :aria-label="labels[currentFile] || currentFile" @input="dirty=true" />
      <div class="production-tools"><button :disabled="busy || !!studio || !dirty" @click="run(async()=>{await saveSources();notice='工程草稿已保存'})"><Save :size="16" />保存草稿</button><button :disabled="busy || !!studio" @click="validate">校验工程</button></div>
    </div>
    <footer><small>剪辑导出复用已有视频。字幕、配音与 AI 后期使用所选模型。</small><button class="production-primary" :disabled="busy || !production || cutDirty" @click="submit('export')"><Film :size="16" />导出当前成片</button></footer>
  </section>
</template>

<style scoped>
.production-workbench{min-width:0;font-variant-numeric:tabular-nums}.production-workbench header{display:flex;align-items:center;justify-content:space-between;gap:20px;margin:22px 0}.production-workbench h3{font-size:22px;letter-spacing:-.5px;margin:8px 0;text-wrap:balance}.production-workbench p{line-height:1.7;color:var(--ink-secondary);font-size:13px}.production-eyebrow{font-size:10px;letter-spacing:1.5px;color:var(--ink-secondary)}.production-workbench button{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:44px;padding:10px 14px;background:var(--glass-inset);color:var(--ink);border:0;border-radius:12px;font:inherit;font-size:13px;cursor:pointer;transition:transform .15s,background .15s}.production-workbench button:active{transform:scale(.96)}.production-workbench button:disabled{opacity:.5;cursor:default}.production-workbench nav{display:flex;gap:6px;overflow-x:auto;margin:16px 0;padding:4px;background:var(--glass-inset);border-radius:16px}.production-workbench nav button{white-space:nowrap;flex:1;background:transparent}.production-workbench nav [aria-pressed=true]{background:color-mix(in srgb,var(--brand) 14%,transparent)}.production-launch{display:grid;justify-items:center;text-align:center;padding:40px 22px;background:var(--glass-inset);border-radius:18px}.production-launch p{max-width:420px}.production-launch h4{margin-bottom:0}.production-workbench .production-primary{background:var(--brand);color:#fff}.production-studio iframe{width:100%;height:75dvh;min-height:460px;border:0;border-radius:12px;background:#17191d}.production-tools{display:flex;flex-wrap:wrap;gap:10px;margin:12px 0}.production-assistant{display:grid;gap:16px;max-width:760px}.production-assistant label{display:grid;gap:8px;font-size:13px}.production-workbench textarea{width:100%;box-sizing:border-box;resize:vertical;padding:14px;border:1px solid var(--glass-edge);border-radius:12px;color:var(--ink);background:var(--glass-inset);font:inherit;line-height:1.7;min-width:0}.production-documents textarea{margin-top:16px;font-size:13px}.production-workbench footer{display:flex;align-items:center;justify-content:space-between;gap:18px;margin-top:24px;padding-top:16px;border-top:1px solid var(--glass-edge)}.production-workbench small{font-size:12px;color:var(--ink-secondary);line-height:1.6}.production-upload{display:flex;align-items:center;justify-content:center;position:relative;gap:10px;min-height:90px;background:var(--glass-inset);border-radius:16px;font-size:13px;cursor:pointer}.production-upload input{position:absolute;inset:0;opacity:0;cursor:pointer;max-width:100%}.production-assets{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;margin-top:18px}.production-assets article{display:flex;gap:12px;padding:16px;background:var(--glass-inset);border-radius:14px;min-width:0}.production-assets article div{display:grid;gap:6px;min-width:0}.production-assets strong{overflow:hidden;text-overflow:ellipsis;font-size:13px}.production-assets code{overflow-wrap:anywhere;font-size:11px;color:var(--ink-secondary)}.production-message{padding:12px 16px;background:var(--glass-inset);border-radius:12px;overflow-wrap:anywhere}.production-message.error{background:#d9494914}.production-workbench :is(button,textarea,input):focus-visible{outline:2px solid var(--brand);outline-offset:3px}@media(max-width:760px){.production-workbench header,.production-workbench footer{align-items:flex-start;flex-direction:column;gap:12px}.production-workbench h3{font-size:20px}.production-workbench footer button{width:100%}.production-workbench nav button{font-size:12px;padding:8px 10px}.production-studio iframe{min-height:480px;height:80dvh}.production-assets{grid-template-columns:minmax(0,1fr)}}
</style>
