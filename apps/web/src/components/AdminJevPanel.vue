<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { LoaderCircle, Network, Save, TestTube2 } from 'lucide-vue-next'
import { api } from '@/lib/api'
import { useToastStore } from '@/stores/toast'

interface Configuration {
  provider: string
  enabled: boolean
  has_api_key: boolean
  model: string
  timeout_seconds: number
  route_confidence: number
  source: string
  endpoint: string
  platforms: Array<{ provider: string; name: string; endpoint: string; default_model: string; model: string; has_api_key: boolean; source: string }>
}
const toast = useToastStore()
const loading = ref(true)
const saving = ref(false)
const testing = ref(false)
const failed = ref(false)
const saved = ref<Configuration | null>(null)
const result = ref('')
const form = reactive({ enabled: false, provider: 'typesafe', api_key: '', clear_api_key: false, model: 'jev-1.13.0', timeout_seconds: 8, route_confidence: 0.65 })
const platform = computed(() => saved.value?.platforms?.find(item => item.provider === form.provider))
const dirty = computed(() => !!saved.value && (form.provider !== saved.value.provider || form.enabled !== saved.value.enabled || !!form.api_key || form.clear_api_key || form.model !== saved.value.model || form.timeout_seconds !== saved.value.timeout_seconds || form.route_confidence !== saved.value.route_confidence))
const busy = computed(() => loading.value || saving.value || testing.value)
function apply(value: Configuration) {
  saved.value = value
  Object.assign(form, { enabled: value.enabled, provider: value.provider, model: value.model, timeout_seconds: value.timeout_seconds, route_confidence: value.route_confidence, api_key: '', clear_api_key: false })
}
function changePlatform() {
  form.api_key = ''
  form.clear_api_key = false
  form.model = platform.value?.model || platform.value?.default_model || 'jev-1.13.0'
  result.value = ''
}
async function load() {
  loading.value = true
  failed.value = false
  try { apply(await api<Configuration>('/admin/jev')) }
  catch (error) { failed.value = true; report(error) }
  finally { loading.value = false }
}
function report(error: unknown) {
  toast.show('JEV 配置操作失败', { tone: 'error', message: error instanceof Error ? error.message : '请稍后重试' })
}
async function save() {
  saving.value = true
  result.value = ''
  try {
    apply(await api<Configuration>('/admin/jev', { method: 'PUT', body: JSON.stringify(form) }))
    toast.show('JEV 配置已保存', { message: '后续新对话请求立即生效，无需重启。' })
  } catch (error) { report(error) }
  finally { saving.value = false }
}
async function test() {
  testing.value = true
  result.value = ''
  try {
    const value = await api<{ model: string; latency_ms: number }>('/admin/jev/test', { method: 'POST' })
    result.value = `连接正常 · ${value.model} · ${value.latency_ms} ms · 文字意图识别通过`
  } catch (error) { report(error) }
  finally { testing.value = false }
}
onMounted(load)
</script>

<template>
  <section class="admin-section jev-panel">
    <header class="section-heading">
      <div><h2><Network :size="22" /> JEV 意图路由</h2><p>首页、项目助手与全自动创作的判断配置，统一管理。</p></div>
      <span class="jev-status">{{ saved?.enabled ? '已启用' : '未启用' }}</span>
    </header>
    <p v-if="loading" role="status"><LoaderCircle :size="18" class="spin" /> 正在读取配置…</p>
    <div v-else-if="failed"><p>配置加载失败，请重试。</p><button class="secondary-button" @click="load">重新加载</button></div>
    <form v-else class="jev-form" @submit.prevent="save">
      <fieldset :disabled="busy">
        <label class="jev-enable"><span><strong>启用智能意图判断</strong><small>覆盖首页与项目对话、剧本、分镜、审核、修复和视频提示词。关闭后使用原流程。</small></span><input v-model="form.enabled" type="checkbox" role="switch" /></label>
        <p class="jev-scope-note">项目意图、创作模块、单镜内部一致性与定点修复范围由 JEV 独立判断，代码执行结果。判断不明确时保留断点、等待确认；原文覆盖、整章逻辑审核和内容生成仍由原模型负责。</p>
        <div class="jev-grid">
          <label class="jev-wide">服务平台<select v-model="form.provider" @change="changePlatform"><option v-for="item in saved?.platforms" :key="item.provider" :value="item.provider">{{ item.name }}</option></select><small>分别保存各平台密钥与模型，切换并保存后生效。OpenCode Zen 可选 jev-1.13-free，额度与可用性以平台为准。</small></label>
          <label class="jev-wide">API Key<input v-model="form.api_key" type="password" autocomplete="new-password" :disabled="form.clear_api_key" :placeholder="platform?.has_api_key ? '已配置，留空保留此平台密钥' : `输入 ${platform?.name || ''} API Key`" /><small>{{ platform?.source === 'environment' ? '当前继承部署配置，保存后由此页面独立管理。' : '密钥加密保存，不回显明文。' }}</small></label>
          <label v-if="platform?.has_api_key" class="jev-clear jev-wide"><input v-model="form.clear_api_key" type="checkbox" @change="form.clear_api_key && (form.api_key = '', form.enabled = false)" /> 清除此平台密钥并关闭 JEV</label>
          <label>判断模型<input v-model.trim="form.model" required pattern="jev-[a-zA-Z0-9.\-]+" list="jev-models" /><datalist id="jev-models"><option v-if="form.provider === 'opencode_zen'" value="jev-1.13-free" /><template v-else><option value="jev-1.13.0" /><option value="jev-latest" /><option value="jev-preview" /></template></datalist><small>填写所选平台支持的 JEV 模型名称。</small></label>
          <label>请求超时（秒）<input v-model.number="form.timeout_seconds" type="number" min="1" max="30" step="1" required /><small>超时后提示重试，不直接发起媒体生成。</small></label>
          <label>媒体意图置信度阈值<input v-model.number="form.route_confidence" type="number" min="0" max="1" step="0.01" required /><small>默认 0.65；越高越谨慎，可能更频繁要求澄清。</small></label>
          <div class="jev-endpoint"><span>平台接口</span><code>{{ platform?.endpoint }}</code></div>
        </div>
      </fieldset>
      <footer class="jev-actions">
        <button class="primary-button" type="submit" :disabled="busy || !dirty"><LoaderCircle v-if="saving" class="spin" :size="16" /><Save v-else :size="16" />保存配置</button>
        <button class="secondary-button" type="button" :disabled="busy || dirty || !saved?.has_api_key" @click="test"><LoaderCircle v-if="testing" class="spin" :size="16" /><TestTube2 v-else :size="16" />测试连接</button>
        <small>{{ dirty ? '请先保存，再测试新配置。' : '测试会发送一次简短判断请求，不会生成图片或视频。' }}</small>
      </footer>
      <p v-if="result" class="jev-result" role="status">{{ result }}</p>
    </form>
  </section>
</template>

<style scoped>
.jev-panel { --text-secondary: var(--ink-secondary); --text-primary: var(--ink); max-width: 1080px; width: 100%; min-width: 0; }
.jev-panel h2 { display: flex; align-items: center; gap: 10px; }
.jev-status { white-space: nowrap; font-size: 13px; color: var(--text-secondary); }
.jev-form { display: grid; gap: 24px; padding: 28px; border-radius: 24px; background: var(--glass-panel, var(--surface)); box-shadow: 0 8px 32px var(--glass-depth, #0001), inset 0 1px 0 var(--glass-edge, #fff3); backdrop-filter: blur(22px); }
.jev-form fieldset { min-width: 0; padding: 0; margin: 0; border: 0; }
.jev-scope-note { margin: 0 0 22px; color: var(--ink-secondary); font-size: 13px; line-height: 1.7; }
.jev-enable { display: flex; align-items: center; justify-content: space-between; gap: 20px; padding: 0 0 24px; }
.jev-enable span { display: grid; gap: 6px; }
.jev-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 22px; }
.jev-grid > label { display: grid; gap: 8px; min-width: 0; }
.jev-grid input:not([type=checkbox]), .jev-grid select { box-sizing: border-box; width: 100%; min-width: 0; min-height: 44px; border: 1px solid var(--glass-edge, var(--line)); border-radius: 12px; padding: 10px 14px; background: var(--glass-inset, var(--surface-subtle)); color: var(--ink); font: inherit; }
.jev-grid input:focus-visible, .jev-grid select:focus-visible { outline: 2px solid var(--brand); outline-offset: 2px; }
.jev-wide { grid-column: 1 / -1; }
.jev-grid .jev-clear { display: flex; align-items: center; min-height: 40px; }
.jev-form small { color: var(--text-secondary); line-height: 1.6; }
.jev-form input[type=checkbox] { width: 20px; height: 20px; flex-shrink: 0; accent-color: var(--brand); }
.jev-endpoint { display: grid; gap: 8px; align-content: start; }
.jev-endpoint code { overflow-wrap: anywhere; color: var(--text-secondary); font-size: 12px; }
.jev-actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.jev-actions button { display: inline-flex; align-items: center; justify-content: center; gap: 8px; min-height: 44px; border: 1px solid var(--glass-edge, var(--line)); padding: 10px 16px; border-radius: 12px; background: var(--glass-hover, var(--surface-subtle)); color: var(--ink); font: inherit; cursor: pointer; }
.jev-actions button:disabled { opacity: .5; cursor: not-allowed; }
.jev-actions .primary-button:not(:disabled) { background: var(--brand); color: white; }
.jev-result { color: var(--text-primary); font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
@media (max-width: 640px) { .jev-form { padding: 18px; border-radius: 20px; margin-bottom: 24px; } .jev-grid { grid-template-columns: minmax(0, 1fr); } .jev-actions button { flex: 1; } .jev-actions small { flex-basis: 100%; } }
</style>
