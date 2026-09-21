<script setup lang="ts">
import { IMAGE_ACCEPT_ATTRIBUTE, MAX_IMAGE_UPLOAD_BYTES, isSupportedImage } from '@/lib/imageUpload'
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { Sparkles, Images, X, LoaderCircle, RotateCcw, BookOpen } from 'lucide-vue-next'
import BaseDialog from '@/components/BaseDialog.vue'
import { api } from '@/lib/api'

interface Job { id: string; status: string; progress: number; latest_message?: string; error_message?: string; result_payload?: { handbook_id?: string }; created_at: string }
const emit = defineEmits<{ created: [id: string] }>()
const open = ref(false)
const images = ref<{ file: File; url: string }[]>([])
const input = ref<HTMLInputElement | null>(null)
const uploading = ref(false)
const error = ref('')
const jobs = ref<Job[]>([])
const active = (job: Job) => ['queued', 'running'].includes(job.status)
const canSubmit = computed(() => images.value.length > 0 && !uploading.value)
let timer: ReturnType<typeof setTimeout> | undefined
let disposed = false
const labels: Record<string, string> = { queued: '排队中', running: '生成中', succeeded: '已完成', failed: '失败', cancelled: '已取消' }

function remove(index: number) { const item = images.value.splice(index, 1)[0]; if (item) URL.revokeObjectURL(item.url) }
function select(event: Event) {
  error.value = ''
  for (const file of Array.from((event.target as HTMLInputElement).files || [])) {
    if (images.value.length >= 4) { error.value = '一次最多上传 4 张参考图片'; break }
    if (!isSupportedImage(file)) { error.value = '请选择 JPG、PNG 或 WebP 图片'; continue }
    if (file.size > MAX_IMAGE_UPLOAD_BYTES) { error.value = '单张图片不能超过 100MB'; continue }
    images.value.push({ file, url: URL.createObjectURL(file) })
  }
  if (input.value) input.value.value = ''
}
async function refresh() {
  if (timer) clearTimeout(timer)
  try {
    const result = await api<{ items: Job[] }>('/tasks?task_type=visual_handbook_generation&limit=20')
    if (!disposed) jobs.value = result.items
  } catch (e) { error.value = e instanceof Error ? e.message : '读取任务失败' }
  finally { if (!disposed && open.value) timer = setTimeout(refresh, 3000) }
}
async function submit() {
  if (!canSubmit.value) return
  uploading.value = true; error.value = ''
  try {
    const body = new FormData()
    images.value.forEach(item => body.append('files', item.file))
    await api('/admin/handbooks/ai-create', { method: 'POST', body })
    while (images.value.length) remove(0)
    await refresh()
  } catch (e) { error.value = e instanceof Error ? e.message : '创建失败' }
  finally { uploading.value = false }
}
async function act(job: Job, action: 'retry' | 'cancel') {
  try { await api(`/tasks/${job.id}/${action}`, { method: 'POST' }); await refresh() }
  catch (e) { error.value = e instanceof Error ? e.message : '操作失败' }
}
function edit(job: Job) { if (job.result_payload?.handbook_id) { emit('created', job.result_payload.handbook_id); open.value = false } }
watch(open, value => { if (value) void refresh(); else if (timer) clearTimeout(timer) })
onBeforeUnmount(() => { disposed = true; if (timer) clearTimeout(timer); images.value.forEach(item => URL.revokeObjectURL(item.url)) })
</script>

<template>
  <button class="button button--secondary" type="button" @click="open = true"><Sparkles :size="17" />AI 创建画风</button>
  <BaseDialog :open="open" title="从参考图创建画风手册" description="上传图片，AI 逐图分析并生成完整画风技能包。" wide @update:open="open = $event">
    <div class="style-creator">
      <p>上传 1–4 张同风格图片。AI 将拆解线条、色彩、五官妆容、材质、光影与构图，生成 12 份可编辑的手册文件。首图作为封面；多图风格不同时以首图为主。</p>
      <input ref="input" type="file" :accept="IMAGE_ACCEPT_ATTRIBUTE" multiple hidden @change="select" />
      <div class="style-references">
        <figure v-for="(item, index) in images" :key="item.url"><img :src="item.url" :alt="`参考图 ${index + 1}`" /><figcaption>{{ index === 0 ? '主参考 · 封面' : `参考图 ${index + 1}` }}</figcaption><button type="button" :disabled="uploading" :aria-label="`移除参考图 ${index + 1}`" @click="remove(index)"><X :size="16" /></button></figure>
        <button v-if="images.length < 4" class="style-add" :disabled="uploading" @click="input?.click()"><Images :size="26" /><span>添加参考图</span><small>单图 ≤ 100MB</small></button>
      </div>
      <p v-if="error" class="style-error" role="alert">{{ error }}</p>
      <button class="button button--primary" :disabled="!canSubmit" @click="submit"><LoaderCircle v-if="uploading" class="spin" :size="17" /><Sparkles v-else :size="17" />{{ uploading ? '正在上传参考图…' : '分析并生成手册' }}</button>
      <small>使用默认文本模型的图片理解能力。生成在后台继续；文本手册能提炼风格细节，最终还原效果仍取决于所选生图模型及参考图支持。</small>
      <div v-if="jobs.length" class="style-jobs"><h3>创建记录</h3><article v-for="job in jobs" :key="job.id">
        <header><strong>{{ labels[job.status] || job.status }}</strong><time>{{ new Date(job.created_at).toLocaleString() }}</time></header>
        <p role="status">{{ job.error_message || job.latest_message || '正在准备画风任务' }}</p>
        <progress v-if="active(job)" :value="job.progress" max="100" />
        <button v-if="active(job)" class="button button--ghost" @click="act(job, 'cancel')">取消任务</button>
        <button v-else-if="job.status === 'succeeded'" class="button button--secondary" @click="edit(job)"><BookOpen :size="16" />查看与编辑手册</button>
        <button v-else class="button button--ghost" @click="act(job, 'retry')"><RotateCcw :size="16" />继续重试</button>
      </article></div>
    </div>
  </BaseDialog>
</template>

<style scoped>
.style-creator{display:grid;gap:16px;color:var(--ink);min-width:0}.style-creator p{margin:0;line-height:1.7;color:var(--ink-secondary);overflow-wrap:anywhere}.style-creator small{color:var(--ink-tertiary);line-height:1.6}.style-references{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.style-references figure{position:relative;margin:0;min-width:0}.style-references img{width:100%;height:140px;object-fit:contain;background:var(--surface-subtle);border-radius:12px;outline:1px solid rgba(255,255,255,.1);outline-offset:-1px}.style-references figcaption{font-size:12px;margin-top:6px}.style-references figure button{position:absolute;top:4px;right:4px;width:40px;height:40px;border:0;border-radius:10px;background:var(--surface);color:var(--ink);display:grid;place-items:center}.style-add{height:140px;border:1px dashed var(--line-strong);border-radius:12px;background:var(--surface-subtle);color:var(--ink-secondary);display:grid;justify-items:center;align-content:center;gap:7px}.style-creator button{cursor:pointer;transition:transform .18s,background-color .18s;min-height:40px}.style-creator button:active:not(:disabled){transform:scale(.96)}.style-creator button:disabled{opacity:.5;cursor:not-allowed}.style-creator .style-error{color:var(--danger)}.style-jobs{display:grid;gap:12px}.style-jobs h3{margin:12px 0 0}.style-jobs article{padding:16px;display:grid;gap:10px;background:var(--surface-subtle);border-radius:16px;box-shadow:inset 0 0 0 1px var(--line)}.style-jobs header{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap}.style-jobs time{font-size:12px;color:var(--ink-tertiary)}.style-jobs progress{width:100%;height:5px;accent-color:var(--brand)}.style-jobs{font-variant-numeric:tabular-nums}:global(:root[data-theme='light']) .style-references img{outline-color:rgba(0,0,0,.1)}
@media(max-width:600px){.style-references{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(prefers-reduced-motion:reduce){.style-creator button{transition:none}}
</style>
