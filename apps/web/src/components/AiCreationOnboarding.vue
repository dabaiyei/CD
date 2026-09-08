<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { Check, LoaderCircle, Sparkles } from 'lucide-vue-next'
import BaseDialog from '@/components/BaseDialog.vue'
import { api } from '@/lib/api'

const props = defineProps<{ projectId: string }>()
const emit = defineEmits<{ complete: [] }>()
interface Proposal { title: string; introduction: string; premise: string }
interface Preferences { genre: string; chapter_count: number; chapter_duration_seconds: number; feedback: string }
interface State { phase?: string; revision?: number; proposals?: Proposal[]; preferences?: Preferences }
interface Result { state: State; task_status: string | null; error: string | null; progress?: number; message?: string }
const state = ref<State>({})
const taskStatus = ref<string | null>(null)
const error = ref('')
const progress = ref(0)
const progressMessage = ref('')
const open = ref(true)
const submitting = ref(false)
const loaded = ref(false)
const preferences = reactive<Preferences>({ genre: '', chapter_count: 12, chapter_duration_seconds: 120, feedback: '' })
const busy = computed(() => ['queued', 'running'].includes(taskStatus.value || ''))
const planning = computed(() => state.value.phase === 'outline' && busy.value)
let timer: ReturnType<typeof setTimeout> | undefined
let disposed = false

function apply(result: Result): void {
  if ((result.state.revision || 0) < (state.value.revision || 0)) return
  state.value = result.state
  taskStatus.value = result.task_status
  error.value = result.error || ''
  if (result.progress !== undefined) progress.value = result.progress
  if (result.message !== undefined) progressMessage.value = result.message
  if (!loaded.value && result.state.preferences) Object.assign(preferences, result.state.preferences)
  loaded.value = true
  if (result.state.phase === 'ready') { open.value = false; emit('complete') }
}

async function refresh(): Promise<void> {
  try {
    const result = await api<Result>(`/projects/${props.projectId}/ai-creation`)
    if (!disposed) apply(result)
  } catch (reason) {
    if (!disposed) error.value = reason instanceof Error ? reason.message : '暂时无法读取创作进度'
  } finally {
    if (!disposed && state.value.phase !== 'ready') timer = setTimeout(refresh, 2500)
  }
}

async function submit(action: 'propose' | 'choose' | 'retry', proposalIndex?: number): Promise<void> {
  if (submitting.value || busy.value) return
  submitting.value = true
  error.value = ''
  try {
    const result = await api<Result>(`/projects/${props.projectId}/ai-creation`, {
      method: 'POST', body: JSON.stringify({ action, revision: state.value.revision || 0,
        preferences: action === 'propose' ? preferences : undefined, proposal_index: proposalIndex }),
    })
    if (!disposed) apply(result)
  } catch (reason) { error.value = reason instanceof Error ? reason.message : '提交失败，请重试' }
  finally { submitting.value = false }
}
onMounted(refresh)
onBeforeUnmount(() => { disposed = true; clearTimeout(timer) })
</script>

<template>
  <section class="ai-creation-entry">
    <div v-if="planning" class="ai-creation-loading" role="status" aria-live="polite">
      <Sparkles class="ai-creation-orbit" :size="36" />
      <h2>让故事逐章展开</h2>
      <p aria-live="polite">{{ progressMessage || '正在规划故事结构' }}</p>
      <progress :value="progress" max="100" aria-label="故事规划进度" />
      <p>正在规划故事大纲、人物设定与章节。你可以离开，稍后回来继续。</p>
    </div>
    <button v-else class="button button--primary" @click="open = true"><Sparkles :size="18" />继续 AI 创作</button>
    <BaseDialog :open="open && !planning" title="一起创造一个故事" description="先确定创作方向，再挑选你最想拍的故事。" wide @update:open="open = $event">
      <div class="ai-creation-flow">
        <p v-if="!loaded" role="status">正在恢复创作进度…</p>
        <template v-else>
          <div v-if="busy" class="ai-creation-thinking" role="status"><LoaderCircle class="spin" :size="20" />正在构思不同的故事方向…</div>
          <div v-if="state.proposals?.length && !busy" class="ai-creation-proposals">
            <article v-for="(proposal, index) in state.proposals" :key="`${state.revision}-${index}`" class="ai-creation-proposal">
              <span class="ai-creation-index">方向 {{ index + 1 }}</span>
              <h3>{{ proposal.title }}</h3><p>{{ proposal.introduction }}</p><p>{{ proposal.premise }}</p>
              <button class="button button--primary" :disabled="submitting" @click="submit('choose', index)"><Check :size="17" />就创作这个故事</button>
            </article>
          </div>
          <form v-if="!busy" class="ai-creation-form" @submit.prevent="submit('propose')">
            <label>想创作什么类型？<input v-model.trim="preferences.genre" required maxlength="300" placeholder="例如：都市悬疑，带一点黑色幽默" /></label>
            <div class="ai-creation-fields">
              <label>大约多少章？<input v-model.number="preferences.chapter_count" type="number" required min="1" max="100" /></label>
              <label>每章成片多久（秒）？<input v-model.number="preferences.chapter_duration_seconds" type="number" required min="15" max="7200" /></label>
            </div>
            <label>{{ state.proposals?.length ? '都不满意？告诉我调整方向' : '还有哪些想法？' }}<textarea v-model.trim="preferences.feedback" rows="3" maxlength="6000" placeholder="人物、世界观、故事气质，或你不想出现的内容" /></label>
            <button class="button button--primary" :disabled="submitting || !loaded"><Sparkles :size="17" />{{ state.proposals?.length ? '按新意见重新构思' : '确认方向，构思故事' }}</button>
          </form>
          <div v-if="error" role="alert" class="ai-creation-error"><p>{{ error }}</p><button v-if="['failed', 'cancelled'].includes(taskStatus || '')" class="button button--secondary" :disabled="submitting" @click="submit('retry')">保留当前选择并重试</button></div>
        </template>
      </div>
    </BaseDialog>
  </section>
</template>

<style scoped>
.ai-creation-entry { width: 100%; padding: 24px; }
.ai-creation-flow, .ai-creation-form, .ai-creation-form label { display: grid; gap: 16px; min-width: 0; }
.ai-creation-form label { gap: 8px; }
.ai-creation-fields, .ai-creation-proposals { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 240px), 1fr)); gap: 16px; }
.ai-creation-proposal { padding: 20px; border-radius: 20px; background: var(--surface); color: var(--ink); box-shadow: 0 2px 12px #0000000a, 0 0 0 1px var(--line); }
.ai-creation-proposal p { line-height: 1.7; overflow-wrap: anywhere; }
.ai-creation-proposal h3 { text-wrap: balance; }
.ai-creation-index { color: var(--brand); font-variant-numeric: tabular-nums; }
.ai-creation-form input, .ai-creation-form textarea { width: 100%; box-sizing: border-box; padding: 12px 14px; border-radius: 12px; border: 1px solid var(--line); color: var(--ink); background: var(--surface); font: inherit; }
.ai-creation-flow button { min-height: 42px; transition: transform 180ms ease, opacity 180ms ease; }
.ai-creation-flow button:active { transform: scale(.96); }
.ai-creation-loading { min-height: 280px; display: grid; place-content: center; justify-items: center; text-align: center; color: var(--ink); }
.ai-creation-thinking { display: flex; align-items: center; gap: 10px; padding: 20px 0; }
.ai-creation-error { color: var(--danger, #d04444); }
.ai-creation-orbit { animation: creation-breathe 2s ease-in-out infinite; color: var(--brand); }
@keyframes creation-breathe { 50% { transform: scale(1.12) rotate(8deg); opacity: .55; } }
@media (prefers-reduced-motion: reduce) { .ai-creation-orbit { animation: none; } .ai-creation-flow button { transition: none; } }
</style>
