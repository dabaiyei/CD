<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import {
  BadgeCheck,
  Boxes,
  BrainCircuit,
  Film,
  Layers3,
  LoaderCircle,
  Pencil,
  Plus,
  ScanSearch,
  ScrollText,
  Sparkles,
  Trash2,
  WandSparkles,
} from 'lucide-vue-next'

import BaseDialog from '@/components/BaseDialog.vue'
import { api } from '@/lib/api'
import { useToastStore } from '@/stores/toast'
import type { UserSkill, UserSkillStage, UserSkillStageOption } from '@/types'

const toast = useToastStore()
const skills = ref<UserSkill[]>([])
const stageOptions = ref<UserSkillStageOption[]>([])
const loading = ref(true)
const saving = ref(false)
const deleting = ref(false)
const dialogOpen = ref(false)
const editingId = ref<string | null>(null)
const deleteTarget = ref<UserSkill | null>(null)
const stageFilter = ref<'all' | UserSkillStage>('all')

const stageVisuals = {
  script_generation: { label: '剧本生成', icon: ScrollText, tone: 'teal' },
  script_review: { label: '剧本审核与修复', icon: BadgeCheck, tone: 'green' },
  asset_extraction: { label: '资产提取', icon: Boxes, tone: 'amber' },
  asset_prompt_generation: { label: '资产提示词生成', icon: WandSparkles, tone: 'rose' },
  storyboard_generation: { label: '分镜生成', icon: Layers3, tone: 'blue' },
  storyboard_review: { label: '分镜审核与修复', icon: ScanSearch, tone: 'cyan' },
  video_generation: { label: '视频提示词与视频生成', icon: Film, tone: 'violet' },
} satisfies Record<UserSkillStage, { label: string; icon: typeof ScrollText; tone: string }>

const emptyForm = () => ({
  name: '',
  trigger_stages: [] as UserSkillStage[],
  description: '',
  enabled: true,
})
const form = reactive(emptyForm())
const enabledCount = computed(() => skills.value.filter((skill) => skill.enabled).length)
const stageCoverage = computed(() => new Set(skills.value.flatMap((skill) => skill.trigger_stages)).size)
const visibleSkills = computed(() => stageFilter.value === 'all'
  ? skills.value
  : skills.value.filter((skill) => skill.trigger_stages.includes(stageFilter.value as UserSkillStage)))
const canSave = computed(() => Boolean(
  form.name.trim() && form.description.trim() && form.trigger_stages.length,
))

onMounted(loadSkills)

async function loadSkills(): Promise<void> {
  loading.value = true
  try {
    const [skillRows, stages] = await Promise.all([
      api<UserSkill[]>('/user-skills'),
      api<UserSkillStageOption[]>('/user-skills/stages'),
    ])
    skills.value = skillRows
    stageOptions.value = stages
  } catch (error) {
    toast.show('个人 Skills 加载失败', {
      message: error instanceof Error ? error.message : undefined,
      tone: 'error',
    })
  } finally {
    loading.value = false
  }
}

function resetForm(): void {
  Object.assign(form, emptyForm())
}

function openCreate(): void {
  editingId.value = null
  resetForm()
  dialogOpen.value = true
}

function openEdit(skill: UserSkill): void {
  editingId.value = skill.id
  Object.assign(form, {
    name: skill.name,
    trigger_stages: [...skill.trigger_stages],
    description: skill.description,
    enabled: skill.enabled,
  })
  dialogOpen.value = true
}

function toggleFormStage(stage: UserSkillStage): void {
  form.trigger_stages = form.trigger_stages.includes(stage)
    ? form.trigger_stages.filter((item) => item !== stage)
    : [...form.trigger_stages, stage]
}

async function saveSkill(): Promise<void> {
  if (!canSave.value || saving.value) return
  saving.value = true
  try {
    const payload = {
      name: form.name.trim(),
      trigger_stages: form.trigger_stages,
      description: form.description.trim(),
      enabled: form.enabled,
    }
    const saved = editingId.value
      ? await api<UserSkill>(`/user-skills/${editingId.value}`, {
          method: 'PATCH',
          body: JSON.stringify(payload),
        })
      : await api<UserSkill>('/user-skills', {
          method: 'POST',
          body: JSON.stringify(payload),
        })
    const index = skills.value.findIndex((item) => item.id === saved.id)
    if (index >= 0) skills.value.splice(index, 1, saved)
    else skills.value.unshift(saved)
    dialogOpen.value = false
    toast.show(editingId.value ? 'Skill 已更新' : 'Skill 已学会', {
      message: `${saved.name} 将在匹配的创作阶段按需检索`,
      tone: 'success',
    })
  } catch (error) {
    toast.show('Skill 保存失败', {
      message: error instanceof Error ? error.message : undefined,
      tone: 'error',
    })
  } finally {
    saving.value = false
  }
}

async function toggleSkill(skill: UserSkill): Promise<void> {
  const previous = skill.enabled
  skill.enabled = !previous
  try {
    const updated = await api<UserSkill>(`/user-skills/${skill.id}`, {
      method: 'PATCH',
      body: JSON.stringify({ enabled: skill.enabled }),
    })
    Object.assign(skill, updated)
    toast.show(updated.enabled ? 'Skill 已启用' : 'Skill 已停用', {
      message: updated.enabled ? '匹配阶段的 AI 任务现在可以检索它' : 'AI 不会再把它作为生效规则',
      tone: updated.enabled ? 'success' : 'info',
    })
  } catch (error) {
    skill.enabled = previous
    toast.show('状态更新失败', {
      message: error instanceof Error ? error.message : undefined,
      tone: 'error',
    })
  }
}

async function deleteSkill(): Promise<void> {
  const target = deleteTarget.value
  if (!target || deleting.value) return
  deleting.value = true
  try {
    await api(`/user-skills/${target.id}`, { method: 'DELETE' })
    skills.value = skills.value.filter((item) => item.id !== target.id)
    deleteTarget.value = null
    toast.show('Skill 已删除', { message: target.name, tone: 'success' })
  } catch (error) {
    toast.show('Skill 删除失败', {
      message: error instanceof Error ? error.message : undefined,
      tone: 'error',
    })
  } finally {
    deleting.value = false
  }
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', { month: 'short', day: 'numeric' }).format(new Date(value))
}
</script>

<template>
  <div class="user-skills-page page-stack">
    <header class="page-header skills-hero">
      <div class="skills-hero__content">
        <span class="eyebrow">PERSONAL CAPABILITIES</span>
        <h1>我的 Skills</h1>
        <p>把你的导演经验、画面技法和生产方法，沉淀为 AI 随时可调用的能力。</p>
      </div>
      <button class="button skills-hero__action" type="button" @click="openCreate">
        <Plus :size="18" /><span>添加 Skill</span>
      </button>
    </header>

    <section class="skill-overview" aria-label="个人 Skill 概览">
      <div><strong class="tabular-nums">{{ skills.length }}</strong><span>已学能力</span></div>
      <div><strong class="tabular-nums">{{ enabledCount }}</strong><span>正在生效</span></div>
      <div><strong class="tabular-nums">{{ stageCoverage }}/7</strong><span>覆盖阶段</span></div>
      <p><BrainCircuit :size="18" />AI 会按当前制作阶段检索这些方法。</p>
    </section>

    <div class="skills-workspace-layout">
      <aside class="skills-stage-rail">
        <nav class="skill-filter-bar" aria-label="按创作阶段筛选">
          <button :class="{ active: stageFilter === 'all' }" type="button" @click="stageFilter = 'all'">
            <Sparkles :size="15" /><span>全部能力</span><small class="tabular-nums">{{ skills.length }}</small>
          </button>
          <button
            v-for="stage in stageOptions"
            :key="stage.value"
            :class="{ active: stageFilter === stage.value }"
            type="button"
            @click="stageFilter = stage.value"
          >
            <component :is="stageVisuals[stage.value].icon" :size="15" /><span>{{ stage.label }}</span>
            <small class="tabular-nums">{{ skills.filter((skill) => skill.trigger_stages.includes(stage.value)).length }}</small>
          </button>
        </nav>
      </aside>

      <main class="skills-catalog">
        <header><div><span>CAPABILITY LIBRARY</span><h2>{{ stageFilter === 'all' ? '全部能力' : stageVisuals[stageFilter].label }}</h2></div><strong class="tabular-nums">{{ visibleSkills.length }}</strong></header>

        <section v-if="loading" class="user-skill-grid" aria-label="正在加载个人 Skills">
          <div v-for="index in 6" :key="index" class="user-skill-skeleton"><i></i><i></i><i></i></div>
        </section>

        <section v-else-if="visibleSkills.length" class="user-skill-grid" aria-label="个人 Skills">
      <article
        v-for="(skill, index) in visibleSkills"
        :key="skill.id"
        v-motion="{ preset: 'card', index }"
        class="user-skill-card"
        :class="{ 'is-disabled': !skill.enabled }"
      >
        <header>
          <span class="user-skill-card__mark"><BrainCircuit :size="19" /></span>
          <span class="user-skill-card__status" :class="{ active: skill.enabled }">
            {{ skill.enabled ? '已启用' : '已停用' }}
          </span>
          <button class="skill-switch" :class="{ active: skill.enabled }" type="button" :aria-label="skill.enabled ? '停用 Skill' : '启用 Skill'" :aria-pressed="skill.enabled" @click="toggleSkill(skill)">
            <span></span>
          </button>
        </header>
        <div class="user-skill-card__body">
          <h2>{{ skill.name }}</h2>
          <p>{{ skill.description }}</p>
          <div class="user-skill-card__stages">
            <span v-for="stage in skill.trigger_stages" :key="stage" :data-tone="stageVisuals[stage].tone">
              <component :is="stageVisuals[stage].icon" :size="12" />{{ stageVisuals[stage].label }}
            </span>
          </div>
        </div>
        <footer>
          <span class="tabular-nums">v{{ skill.version }} · {{ formatDate(skill.updated_at) }}</span>
          <div>
            <button type="button" title="编辑 Skill" @click="openEdit(skill)"><Pencil :size="15" /></button>
            <button class="danger" type="button" title="删除 Skill" @click="deleteTarget = skill"><Trash2 :size="15" /></button>
          </div>
        </footer>
      </article>
        </section>

        <section v-else class="user-skills-empty">
          <span><BrainCircuit :size="24" /></span>
          <h2>{{ skills.length ? '这个阶段还没有 Skill' : '你的 AI 还没有个人 Skill' }}</h2>
          <p>{{ skills.length ? '切换创作阶段，或为当前阶段添加一个新能力。' : '添加一套常用规则，后续创作时无需反复粘贴。' }}</p>
          <button v-if="!skills.length" class="button button--secondary" type="button" @click="openCreate"><Plus :size="16" />添加第一个 Skill</button>
        </section>
      </main>
    </div>

    <BaseDialog
      v-model:open="dialogOpen"
      :title="editingId ? '编辑个人 Skill' : '添加个人 Skill'"
      description="完整说明只会在 AI 选择该 Skill 后按需读取"
      wide
    >
      <form id="user-skill-form" class="user-skill-form" @submit.prevent="saveSkill">
        <label class="skill-form-field">
          <span>Skill 名称 <i>必填</i></span>
          <div><BrainCircuit :size="17" /><input v-model.trim="form.name" required maxlength="120" placeholder="例如：人物近身打斗分镜技法" /></div>
        </label>

        <fieldset>
          <legend>AI 在何时检索 <i>至少选择一个</i></legend>
          <div class="skill-stage-picker">
            <button
              v-for="stage in stageOptions"
              :key="stage.value"
              :class="{ active: form.trigger_stages.includes(stage.value) }"
              type="button"
              :aria-pressed="form.trigger_stages.includes(stage.value)"
              @click="toggleFormStage(stage.value)"
            >
              <span><component :is="stageVisuals[stage.value].icon" :size="17" /></span>
              <strong>{{ stage.label }}</strong>
              <BadgeCheck :size="16" />
            </button>
          </div>
        </fieldset>

        <label class="skill-form-field skill-form-field--description">
          <span>Skill 技能说明 <i>必填</i></span>
          <div><ScrollText :size="17" /><textarea v-model.trim="form.description" required maxlength="200000" rows="10" placeholder="写清适用条件、执行步骤、必须遵守的规则、输出要求与禁忌。也可以直接粘贴完整 Skill 文本。"></textarea></div>
          <small class="tabular-nums">{{ form.description.length.toLocaleString('zh-CN') }} / 200,000</small>
        </label>

        <label class="skill-enable-row">
          <span><strong>保存后立即启用</strong><small>停用后仍保留内容，但 AI 不会把它作为生效规则。</small></span>
          <input v-model="form.enabled" type="checkbox" />
          <i :class="{ active: form.enabled }"><b></b></i>
        </label>
      </form>
      <template #footer>
        <button class="button button--ghost" type="button" :disabled="saving" @click="dialogOpen = false">取消</button>
        <button class="button button--primary" type="submit" form="user-skill-form" :disabled="!canSave || saving">
          <LoaderCircle v-if="saving" class="spin" :size="17" />
          <Sparkles v-else :size="17" />{{ editingId ? '保存修改' : '让 AI 学会' }}
        </button>
      </template>
    </BaseDialog>

    <BaseDialog :open="Boolean(deleteTarget)" title="删除个人 Skill" description="删除后 AI 将无法再检索这项能力" @update:open="!$event && !deleting && (deleteTarget = null)">
      <div class="danger-confirm">
        <span><Trash2 :size="22" /></span>
        <div><strong>{{ deleteTarget?.name }}</strong><p>此操作不可恢复，但不会影响已经生成的项目内容。</p></div>
      </div>
      <template #footer>
        <button class="button button--ghost" type="button" :disabled="deleting" @click="deleteTarget = null">取消</button>
        <button class="button button--danger" type="button" :disabled="deleting" @click="deleteSkill">
          <LoaderCircle v-if="deleting" class="spin" :size="17" /><Trash2 v-else :size="17" />确认删除
        </button>
      </template>
    </BaseDialog>
  </div>
</template>

<style scoped>
.user-skills-page { gap: 24px; }
.skill-overview { display: grid; grid-template-columns: repeat(3, max-content) minmax(280px, 1fr); align-items: center; gap: clamp(24px, 4vw, 52px); min-height: 78px; padding: 0 4px 18px; border-bottom: 1px solid var(--line); }
.skill-overview > div { display: grid; gap: 3px; }
.skill-overview strong { font-size: 24px; line-height: 1; }
.skill-overview div span { color: var(--ink-tertiary); font-size: 10px; font-weight: 600; }
.skill-overview p { display: flex; justify-self: end; align-items: center; gap: 9px; color: var(--ink-secondary); font-size: 12px; }
.skill-overview p svg { color: var(--brand); }
.skill-filter-bar { display: flex; gap: 7px; overflow-x: auto; padding: 2px 2px 7px; scrollbar-width: thin; }
.skill-filter-bar button { display: inline-flex; min-height: 40px; flex: 0 0 auto; align-items: center; gap: 7px; padding: 0 12px; border: 0; border-radius: 7px; color: var(--ink-secondary); background: var(--surface); box-shadow: var(--shadow-border); cursor: pointer; font-size: 11px; font-weight: 600; transition-property: color, background-color, box-shadow, scale; transition-duration: 150ms; }
.skill-filter-bar button:hover { color: var(--ink); box-shadow: var(--shadow-hover); }
.skill-filter-bar button:active { scale: .96; }
.skill-filter-bar button.active { color: var(--brand-strong); background: var(--brand-soft); box-shadow: inset 0 0 0 1px rgb(8 127 122 / 12%); }
.user-skill-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 310px), 1fr)); gap: 16px; }
.user-skill-card { display: grid; min-height: 268px; grid-template-rows: auto 1fr auto; overflow: hidden; border-radius: 8px; background: var(--surface); box-shadow: var(--shadow-border); transition-property: box-shadow, transform, opacity; transition-duration: 180ms; transition-timing-function: ease-out; }
.user-skill-card:hover { transform: translateY(-2px); box-shadow: var(--shadow-hover); }
.user-skill-card.is-disabled { opacity: .68; }
.user-skill-card > header { display: flex; min-height: 58px; align-items: center; gap: 9px; padding: 10px 14px; border-bottom: 1px solid var(--line); }
.user-skill-card__mark { display: inline-flex; width: 36px; height: 36px; align-items: center; justify-content: center; border-radius: 7px; color: var(--brand-strong); background: var(--brand-soft); }
.user-skill-card__status { color: var(--ink-tertiary); font-size: 10px; font-weight: 700; }
.user-skill-card__status.active { color: var(--success); }
.skill-switch { position: relative; width: 42px; height: 40px; margin-left: auto; padding: 0; border: 0; background: transparent; cursor: pointer; }
.skill-switch::before { position: absolute; top: 12px; left: 4px; width: 34px; height: 18px; border-radius: 9px; background: #d8dce0; content: ''; transition-property: background-color; transition-duration: 180ms; }
.skill-switch span { position: absolute; top: 14px; left: 6px; width: 14px; height: 14px; border-radius: 50%; background: white; box-shadow: 0 1px 4px rgb(0 0 0 / 20%); transition-property: transform; transition-duration: 180ms; transition-timing-function: cubic-bezier(.2, 0, 0, 1); }
.skill-switch.active::before { background: var(--brand); }
.skill-switch.active span { transform: translateX(16px); }
.user-skill-card__body { display: grid; align-content: start; gap: 10px; padding: 18px 16px; }
.user-skill-card__body h2 { font-size: 16px; line-height: 1.35; }
.user-skill-card__body p { display: -webkit-box; overflow: hidden; color: var(--ink-secondary); font-size: 12px; line-height: 1.7; -webkit-box-orient: vertical; -webkit-line-clamp: 4; }
.user-skill-card__stages { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 2px; }
.user-skill-card__stages span { display: inline-flex; min-height: 26px; align-items: center; gap: 4px; padding: 0 8px; border-radius: 5px; color: #35545c; background: #edf5f5; font-size: 9px; font-weight: 600; }
.user-skill-card__stages span[data-tone='amber'] { color: #76561e; background: #fbf2df; }
.user-skill-card__stages span[data-tone='rose'] { color: #8a4d58; background: #faecef; }
.user-skill-card__stages span[data-tone='blue'] { color: #3c5f87; background: #eaf1f8; }
.user-skill-card__stages span[data-tone='violet'] { color: #65578a; background: #f0edf8; }
.user-skill-card > footer { display: flex; min-height: 52px; align-items: center; justify-content: space-between; padding: 7px 10px 7px 16px; border-top: 1px solid var(--line); color: var(--ink-tertiary); font-size: 9px; }
.user-skill-card > footer div { display: flex; gap: 2px; }
.user-skill-card > footer button { display: inline-flex; width: 40px; height: 40px; align-items: center; justify-content: center; border: 0; border-radius: 7px; color: var(--ink-secondary); background: transparent; cursor: pointer; transition-property: color, background-color, scale; transition-duration: 150ms; }
.user-skill-card > footer button:hover { color: var(--ink); background: var(--surface-strong); }
.user-skill-card > footer button.danger:hover { color: var(--danger); background: var(--danger-soft); }
.user-skill-card > footer button:active { scale: .96; }
.user-skills-empty { display: grid; min-height: 310px; place-items: center; align-content: center; gap: 9px; color: var(--ink-secondary); }
.user-skills-empty > span { display: inline-flex; width: 52px; height: 52px; align-items: center; justify-content: center; border-radius: 8px; color: var(--brand); background: var(--brand-soft); }
.user-skills-empty h2 { color: var(--ink); font-size: 16px; }
.user-skills-empty p { font-size: 12px; }
.user-skills-empty .button { margin-top: 8px; }
.user-skill-skeleton { display: grid; min-height: 268px; align-content: start; gap: 14px; padding: 16px; border-radius: 8px; background: var(--surface); box-shadow: var(--shadow-border); }
.user-skill-skeleton i { height: 18px; border-radius: 5px; background: linear-gradient(90deg, #eef0f2, #f7f8f9, #eef0f2); background-size: 200% 100%; animation: skill-shimmer 1.3s linear infinite; }
.user-skill-skeleton i:first-child { width: 38px; height: 38px; }
.user-skill-skeleton i:nth-child(2) { width: 58%; }
.user-skill-skeleton i:nth-child(3) { width: 92%; height: 72px; }
@keyframes skill-shimmer { to { background-position: -200% 0; } }
.user-skill-form { display: grid; gap: 21px; }
.skill-form-field { display: grid; gap: 8px; }
.skill-form-field > span, .user-skill-form legend { color: var(--ink-secondary); font-size: 11px; font-weight: 600; }
.skill-form-field > span i, .user-skill-form legend i { color: var(--danger); font-size: 9px; font-style: normal; }
.skill-form-field > div { display: grid; min-height: 46px; grid-template-columns: 42px 1fr; align-items: start; overflow: hidden; border-radius: 8px; background: var(--surface-subtle); box-shadow: inset 0 0 0 1px rgb(0 0 0 / 8%); transition-property: background-color, box-shadow; transition-duration: 150ms; }
.skill-form-field > div:focus-within { background: var(--surface); box-shadow: inset 0 0 0 1px var(--brand), 0 0 0 3px rgb(8 127 122 / 10%); }
.skill-form-field > div > svg { align-self: start; justify-self: center; margin-top: 14px; color: var(--ink-tertiary); }
.skill-form-field input, .skill-form-field textarea { width: 100%; border: 0; color: var(--ink); background: transparent; outline: 0; }
.skill-form-field input { height: 46px; padding: 0 12px 0 0; }
.skill-form-field textarea { min-height: 210px; resize: vertical; padding: 13px 14px 13px 0; line-height: 1.7; }
.skill-form-field > small { justify-self: end; color: var(--ink-tertiary); font-size: 9px; }
.user-skill-form fieldset { min-width: 0; margin: 0; padding: 0; border: 0; }
.user-skill-form legend { margin-bottom: 9px; }
.skill-stage-picker { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
.skill-stage-picker button { display: grid; min-height: 58px; grid-template-columns: 34px minmax(0, 1fr) 18px; align-items: center; gap: 8px; padding: 6px 10px; border: 0; border-radius: 8px; color: var(--ink-secondary); background: var(--surface-subtle); box-shadow: inset 0 0 0 1px rgb(0 0 0 / 6%); cursor: pointer; text-align: left; transition-property: color, background-color, box-shadow, scale; transition-duration: 150ms; }
.skill-stage-picker button:active { scale: .96; }
.skill-stage-picker button > span { display: inline-flex; width: 34px; height: 34px; align-items: center; justify-content: center; border-radius: 6px; background: var(--surface); }
.skill-stage-picker button strong { overflow: hidden; font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.skill-stage-picker button > svg { opacity: 0; scale: .25; filter: blur(4px); color: var(--brand); transition-property: opacity, scale, filter; transition-duration: 300ms; transition-timing-function: cubic-bezier(.2, 0, 0, 1); }
.skill-stage-picker button.active { color: var(--brand-strong); background: var(--brand-soft); box-shadow: inset 0 0 0 1px rgb(8 127 122 / 16%); }
.skill-stage-picker button.active > svg { opacity: 1; scale: 1; filter: blur(0); }
.skill-enable-row { position: relative; display: flex; min-height: 58px; align-items: center; gap: 16px; padding: 8px 0; cursor: pointer; }
.skill-enable-row > span { display: grid; gap: 4px; margin-right: auto; }
.skill-enable-row strong { font-size: 12px; }
.skill-enable-row small { color: var(--ink-tertiary); font-size: 10px; }
.skill-enable-row input { position: absolute; opacity: 0; }
.skill-enable-row > i { position: relative; width: 40px; height: 22px; flex: 0 0 40px; border-radius: 11px; background: #d8dce0; transition-property: background-color; transition-duration: 180ms; }
.skill-enable-row > i b { position: absolute; top: 3px; left: 3px; width: 16px; height: 16px; border-radius: 50%; background: #fff; box-shadow: 0 1px 4px rgb(0 0 0 / 20%); transition-property: transform; transition-duration: 180ms; }
.skill-enable-row > i.active { background: var(--brand); }
.skill-enable-row > i.active b { transform: translateX(18px); }
@media (prefers-reduced-motion: reduce) { .user-skill-card, .skill-filter-bar button, .skill-switch span, .skill-stage-picker button, .skill-stage-picker button > svg, .skill-enable-row > i, .skill-enable-row > i b { transition-duration: 0ms; } .user-skill-skeleton i { animation: none; } }
@media (max-width: 860px) { .skill-overview { grid-template-columns: repeat(3, 1fr); gap: 18px; } .skill-overview p { grid-column: 1 / -1; justify-self: start; } }
@media (max-width: 560px) { .skill-overview { padding-bottom: 15px; } .skill-overview strong { font-size: 20px; } .skill-overview p { align-items: flex-start; } .skill-stage-picker { grid-template-columns: 1fr; } .user-skill-grid { grid-template-columns: 1fr; } }
</style>
