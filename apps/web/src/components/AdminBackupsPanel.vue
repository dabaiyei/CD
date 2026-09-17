<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Archive, Upload, Download, ShieldCheck, RotateCcw, Trash2, LoaderCircle, LockKeyhole, Database, Files, CheckCircle2, CircleAlert } from 'lucide-vue-next'
import BaseDialog from '@/components/BaseDialog.vue'
import { api, getToken } from '@/lib/api'
import { useToastStore } from '@/stores/toast'

interface Summary { tables: number; records: number; files: number; bytes: number; database: string; counts: Record<string, number>; created_at: string }
interface Job { id: string; kind: string; status: string; progress: number; message: string; created_at: string; downloadable?: boolean; fingerprint?: string; summary?: Summary }
const jobs = ref<Job[]>([])
const error = ref('')
const loading = ref(true)
const busy = ref(false)
const maintenance = ref(false)
const maxBytes = ref(100 * 1024 ** 3)
const adminPassword = ref('')
const backupPassword = ref('')
const input = ref<HTMLInputElement | null>(null)
const uploadProgress = ref<number | null>(null)
const selected = ref<Job | null>(null)
const action = ref<'restore' | 'delete' | null>(null)
const confirmation = ref('')
const toast = useToastStore()
let timer: ReturnType<typeof setTimeout> | undefined
let disposed = false
let uploading: XMLHttpRequest | null = null
const processing = (j: Job) => ['queued', 'uploading', 'running', 'validating', 'recovery_required'].includes(j.status)
const disabled = computed(() => busy.value || maintenance.value || jobs.value.some(processing))
const credentials = computed(() => adminPassword.value.length > 0 && backupPassword.value.length >= 12)
const labels: Record<string, string> = { queued: '排队中', running: '进行中', validating: '校验中', uploading: '上传中', uploaded: '待校验', ready: '可还原', completed: '已完成', failed: '失败', invalid: '校验失败', recovery_required: '待回滚' }
const size = (n: number) => n >= 1024 ** 3 ? `${(n / 1024 ** 3).toFixed(2)} GB` : `${(n / 1024 ** 2).toFixed(1)} MB`
const date = (s: string) => new Date(s).toLocaleString()
const body = () => ({ admin_password: adminPassword.value, backup_password: backupPassword.value })

async function refresh() {
  try {
    const result = await api<{ items: Job[]; maintenance: boolean; max_bytes: number }>('/admin/site-backups')
    if (disposed) return
    jobs.value = result.items
    maintenance.value = result.maintenance
    maxBytes.value = result.max_bytes
    error.value = ''
  } catch (e) { error.value = e instanceof Error ? e.message : '读取备份记录失败' }
  finally {
    loading.value = false
    if (!disposed) timer = setTimeout(refresh, jobs.value.some(processing) ? 2000 : 10000)
  }
}
async function run(operation: () => Promise<unknown>) {
  busy.value = true
  try { await operation(); if (timer) clearTimeout(timer); await refresh() }
  catch (e) { toast.show('操作未完成', { message: e instanceof Error ? e.message : '请稍后重试', tone: 'error' }) }
  finally { busy.value = false }
}
function create() {
  if (!credentials.value || disabled.value) return
  void run(() => api('/admin/site-backups', { method: 'POST', body: JSON.stringify(body()) }))
}
function validate(job: Job) {
  if (!credentials.value) { toast.show('请先填写管理员密码与备份密码'); return }
  void run(() => api(`/admin/site-backups/${job.id}/validate`, { method: 'POST', body: JSON.stringify(body()) }))
}
async function download(job: Job) {
  await run(async () => {
    const result = await api<{ url: string }>(`/admin/site-backups/${job.id}/download-ticket`, { method: 'POST' })
    // Native streamed download: do not buffer multi-gigabyte archives as Blob.
    const a = document.createElement('a')
    a.href = result.url; a.rel = 'noreferrer'; a.download = ''
    document.body.append(a); a.click(); a.remove()
  })
}
function choose(job: Job, kind: 'restore' | 'delete') {
  selected.value = job; action.value = kind; confirmation.value = ''
}
async function confirm() {
  if (!selected.value || !action.value) return
  const job = selected.value
  await run(async () => {
    if (action.value === 'delete') await api(`/admin/site-backups/${job.id}`, { method: 'DELETE' })
    else await api(`/admin/site-backups/${job.id}/restore`, {
      method: 'POST', body: JSON.stringify({ ...body(), fingerprint: job.fingerprint, confirmation: confirmation.value }),
    })
    selected.value = null; action.value = null
  })
}
async function upload(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file) return
  if (file.size > maxBytes.value) { toast.show(`文件不能超过 ${size(maxBytes.value)}`, { tone: 'error' }); return }
  await run(() => new Promise<void>((resolve, reject) => {
    const xhr = new XMLHttpRequest(); uploading = xhr
    xhr.open('POST', '/api/v1/admin/site-backups/upload')
    xhr.setRequestHeader('Authorization', `Bearer ${getToken() || ''}`)
    xhr.setRequestHeader('Content-Type', 'application/octet-stream')
    xhr.upload.onprogress = e => { if (e.lengthComputable) uploadProgress.value = Math.round(e.loaded / e.total * 100) }
    xhr.onload = () => {
      uploadProgress.value = null; uploading = null
      if (xhr.status >= 200 && xhr.status < 300) resolve()
      else {
        let message = `上传失败 (${xhr.status})`
        try { message = JSON.parse(xhr.responseText).detail || message } catch { /* proxy response */ }
        reject(new Error(message))
      }
    }
    xhr.onerror = xhr.onabort = () => { uploadProgress.value = null; uploading = null; reject(new Error('上传中断，请重新上传')) }
    uploadProgress.value = 0; xhr.send(file)
  }))
  if (input.value) input.value.value = ''
}
onMounted(refresh)
onBeforeUnmount(() => { disposed = true; if (timer) clearTimeout(timer); uploading?.abort(); adminPassword.value = ''; backupPassword.value = '' })
</script>

<template>
  <section class="backup-console">
    <header class="backup-heading"><div><h2><Archive :size="23" />备份与还原</h2><p>把完整创作站点安全带走，也能在新服务器上恢复。</p></div><span class="backup-badge"><ShieldCheck :size="16" />加密备份</span></header>
    <p v-if="error" class="backup-notice" role="alert"><CircleAlert :size="18" />{{ error }}</p>
    <template v-else>
      <div class="backup-scopes"><span><Database :size="18" />账号 · 积分 · 项目 · 对话 · 任务</span><span><Files :size="18" />图片 · 视频 · 音频 · 文件</span><span><Archive :size="18" />模型平台 · 提示词 · 手册 · Skills · 记忆</span></div>
      <div class="backup-editor">
        <label><span>当前管理员密码</span><input v-model="adminPassword" type="password" autocomplete="current-password" placeholder="验证操作身份" /></label>
        <label><span>备份密码</span><input v-model="backupPassword" type="password" autocomplete="off" minlength="12" placeholder="至少 12 位，还原时需要此密码" /><small>请单独保存密码，遗失后无法解密。密码不会保存在浏览器。</small></label>
        <div class="backup-actions"><button class="button button--primary" :disabled="disabled || !credentials" @click="create"><Archive :size="17" />创建全站备份</button><button class="button button--ghost" :disabled="disabled" @click="input?.click()"><Upload :size="17" />上传备份文件</button><input ref="input" type="file" accept=".cfbackup" hidden @change="upload" /></div>
      </div>
      <p class="backup-hint">备份和还原期间暂停新请求及任务，已有生成任务不会被强制中断。还原会覆盖全站数据，并先生成当前站点的恢复点。外部 URL 资源保留链接；服务器连接配置保持不变。</p>
      <div v-if="uploadProgress !== null" class="backup-upload" role="status">正在上传 {{ uploadProgress }}%<progress :value="uploadProgress" max="100" /></div>
      <p v-if="maintenance" class="backup-notice"><LockKeyhole :size="17" />站点正在维护操作中，结束后自动恢复访问。</p>
      <div v-if="loading" class="backup-empty"><LoaderCircle class="spin" :size="22" />正在读取备份</div>
      <div v-else-if="!jobs.length" class="backup-empty"><Archive :size="34" /><strong>还没有备份</strong><span>创建第一份备份，保存当前站点的全部创作内容。</span></div>
      <div v-else class="backup-list">
        <article v-for="job in jobs" :key="job.id" class="backup-item">
          <div class="backup-item-heading"><span class="backup-item-icon"><LoaderCircle v-if="processing(job)" class="spin" :size="20" /><CheckCircle2 v-else-if="['ready','completed'].includes(job.status)" :size="20" /><Archive v-else :size="20" /></span><div><strong>{{ job.kind === 'restore' ? '站点还原 / 操作前恢复点' : job.kind === 'upload' ? '上传的站点备份' : '全站备份' }}</strong><small>{{ date(job.created_at) }}</small></div><span class="backup-status" :data-status="job.status">{{ labels[job.status] || job.status }}</span></div>
          <p role="status">{{ job.message }}</p><progress v-if="processing(job)" :value="job.progress" max="100" />
          <div v-if="job.summary" class="backup-stats"><span>{{ job.summary.tables }} 张数据表</span><span>{{ job.summary.records.toLocaleString() }} 条记录</span><span>{{ job.summary.files.toLocaleString() }} 个文件</span><span>{{ size(job.summary.bytes) }}</span></div>
          <div class="backup-row-actions">
            <button v-if="job.downloadable" :disabled="busy || processing(job)" @click="download(job)"><Download :size="16" />{{ job.kind === 'restore' ? '下载恢复点' : '下载' }}</button>
            <button v-if="!processing(job) && (job.downloadable || ['uploaded','invalid'].includes(job.status))" :disabled="disabled" @click="validate(job)"><ShieldCheck :size="16" />校验备份</button>
            <button v-if="job.status === 'ready'" :disabled="disabled || !credentials" @click="choose(job, 'restore')"><RotateCcw :size="16" />还原此备份</button>
            <button v-if="job.status === 'recovery_required'" :disabled="busy || !credentials" @click="run(() => api('/admin/site-backups/recovery/retry', { method: 'POST', body: JSON.stringify(body()) }))"><RotateCcw :size="16" />重试恢复点回滚</button>
            <button v-if="!processing(job)" class="backup-delete" :disabled="disabled" @click="choose(job, 'delete')"><Trash2 :size="16" />删除</button>
          </div>
        </article>
      </div>
    </template>
    <BaseDialog :open="action !== null" :title="action === 'restore' ? '确认覆盖整个站点' : '删除备份文件'" @update:open="value => { if (!value && !busy) action = null }">
      <div v-if="action === 'restore'" class="backup-confirm"><p>将使用这份备份替换<strong>所有账号、项目、对话、媒体、配置与记忆</strong>，包括其他用户的数据。完成后请用备份中的账号重新登录。</p><p>还原前会自动保存恢复点，使用同一个备份密码加密。</p><p v-if="selected?.summary">备份时间：{{ date(selected.summary.created_at) }} · {{ selected.summary.records }} 条记录</p><label>输入“还原整个站点”确认<input v-model="confirmation" placeholder="还原整个站点" autocomplete="off" /></label></div>
      <p v-else>仅删除所选备份包及其操作记录，不删除当前站点内容。删除后不能从本站恢复此备份包。</p>
      <template #footer><button class="button button--ghost" :disabled="busy" @click="action = null">取消</button><button class="button button--primary" :disabled="busy || (action === 'restore' && (confirmation !== '还原整个站点' || !credentials))" @click="confirm"><LoaderCircle v-if="busy" class="spin" :size="16" />{{ action === 'restore' ? '确认还原' : '确认删除' }}</button></template>
    </BaseDialog>
  </section>
</template>

<style scoped>
.backup-console,.backup-confirm{--text:var(--ink);--muted:var(--ink-secondary);--surface-soft:var(--surface-subtle);--border:var(--line);--accent:var(--brand)}
.backup-console{display:grid;gap:20px;min-width:0;color:var(--text)}
.backup-heading,.backup-item-heading{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.backup-heading{justify-content:space-between}.backup-heading h2{display:flex;align-items:center;gap:10px;margin:0;font-size:22px;text-wrap:balance}.backup-heading p,.backup-hint,.backup-item p{color:var(--muted);line-height:1.7}.backup-badge,.backup-status{display:inline-flex;align-items:center;gap:7px;border-radius:20px;padding:7px 12px;background:var(--surface-soft);font-size:12px}.backup-scopes{display:flex;gap:12px 24px;flex-wrap:wrap;color:var(--muted);font-size:13px}.backup-scopes span{display:flex;align-items:center;gap:8px}.backup-editor{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px;padding:22px;background:var(--surface);border-radius:20px;box-shadow:0 4px 20px rgb(0 0 0 / 4%),inset 0 0 0 1px var(--border)}.backup-editor label,.backup-confirm label{display:grid;gap:9px;font-size:13px}.backup-editor small{color:var(--muted);line-height:1.5}.backup-editor input:not([hidden]),.backup-confirm input{width:100%;min-height:46px;box-sizing:border-box;border:1px solid var(--border);border-radius:10px;background:var(--surface-soft);color:var(--text);padding:10px 13px;outline:none;transition:border-color .18s,box-shadow .18s}.backup-editor input:focus,.backup-confirm input:focus{border-color:var(--accent);box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 14%,transparent)}.backup-actions{grid-column:1/-1;display:flex;gap:10px;flex-wrap:wrap}.backup-hint{font-size:13px;margin:0}.backup-list{display:grid;gap:14px}.backup-item{min-width:0;padding:20px;border-radius:18px;background:var(--surface);box-shadow:inset 0 0 0 1px var(--border)}.backup-item-heading>div{display:grid;gap:5px;flex:1}.backup-item-heading small{font-size:12px;color:var(--muted)}.backup-item-icon{display:grid;place-items:center;width:42px;height:42px;background:var(--surface-soft);border-radius:12px;color:var(--accent)}.backup-item p{font-size:13px;overflow-wrap:anywhere}.backup-stats{display:flex;gap:8px 18px;flex-wrap:wrap;font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}.backup-row-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}.backup-row-actions button{display:inline-flex;align-items:center;gap:6px;min-height:40px;padding:8px 12px;border:0;border-radius:9px;color:var(--text);background:var(--surface-soft);cursor:pointer;transition:background-color .16s,transform .16s}.backup-console button:active:not(:disabled){transform:scale(.96)}.backup-console button:disabled{opacity:.45;cursor:not-allowed}.backup-row-actions .backup-delete{margin-left:auto;color:var(--danger)}.backup-row-actions button:hover:not(:disabled){background:color-mix(in srgb,var(--accent) 10%,var(--surface))}.backup-notice{display:flex;align-items:center;gap:10px;padding:14px;border-radius:12px;background:var(--surface-soft);line-height:1.6;font-size:13px}.backup-empty{display:grid;justify-items:center;gap:12px;padding:50px 16px;color:var(--muted);text-align:center}.backup-empty strong{color:var(--text)}progress{width:100%;height:6px;accent-color:var(--accent);border:0;border-radius:6px}.backup-upload{display:grid;gap:10px;font-variant-numeric:tabular-nums}.backup-confirm{display:grid;gap:10px;line-height:1.7}.backup-status[data-status=failed],.backup-status[data-status=invalid]{color:var(--danger)}.backup-status[data-status=ready],.backup-status[data-status=completed]{color:var(--success)}
@media(max-width:640px){.backup-editor{grid-template-columns:minmax(0,1fr);padding:16px}.backup-item{padding:16px}.backup-actions .button{flex:1;min-width:140px}.backup-item-heading strong{font-size:14px}.backup-row-actions .backup-delete{margin-left:0}.backup-heading h2{font-size:20px}}
@media(prefers-reduced-motion:reduce){.backup-console button,.backup-editor input,.backup-confirm input{transition:none}}
</style>
