<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import {
  Check,
  CircleOff,
  Clipboard,
  Coins,
  Copy,
  ExternalLink,
  Gift,
  Link2,
  LoaderCircle,
  MailPlus,
  Pencil,
  Plus,
  QrCode,
  Save,
  ShieldCheck,
  Trash2,
  UserCheck,
  UsersRound,
} from 'lucide-vue-next'
import QRCode from 'qrcode'

import BaseDialog from '@/components/BaseDialog.vue'
import { api } from '@/lib/api'
import { useToastStore } from '@/stores/toast'
import type { Invitation, InvitationSettings } from '@/types'

type DialogMode = 'form' | 'qr' | 'delete' | null

const toast = useToastStore()
const loading = ref(true)
const saving = ref(false)
const settingsSaving = ref(false)
const actionId = ref<string | null>(null)
const invitations = ref<Invitation[]>([])
const urlPrefix = ref('')
const savedUrlPrefix = ref('')
const dialog = ref<DialogMode>(null)
const selected = ref<Invitation | null>(null)
const qrDataUrl = ref('')
const form = reactive({ id: '', name: '', max_registrations: 1, initial_credits: '0.00', enabled: true })

const enabledCount = computed(() => invitations.value.filter((item) => item.enabled).length)
const registeredCount = computed(() => invitations.value.reduce((sum, item) => sum + item.registration_count, 0))
const remainingCount = computed(() => invitations.value.reduce((sum, item) => sum + item.remaining_registrations, 0))
const settingsDirty = computed(() => urlPrefix.value.trim().replace(/\/$/, '') !== savedUrlPrefix.value)

function formatCredits(value: string): string {
  return Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

function formatTime(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(new Date(value))
}

async function loadData(): Promise<void> {
  loading.value = true
  try {
    const [settings, rows] = await Promise.all([
      api<InvitationSettings>('/admin/invitations/settings'),
      api<Invitation[]>('/admin/invitations'),
    ])
    invitations.value = rows
    urlPrefix.value = settings.url_prefix
    savedUrlPrefix.value = settings.url_prefix
  } catch (error) {
    toast.show('邀请码数据加载失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    loading.value = false
  }
}

async function saveSettings(): Promise<void> {
  settingsSaving.value = true
  try {
    const result = await api<InvitationSettings>('/admin/invitations/settings', {
      method: 'PUT',
      body: JSON.stringify({ url_prefix: urlPrefix.value }),
    })
    urlPrefix.value = result.url_prefix
    savedUrlPrefix.value = result.url_prefix
    toast.show('邀请链接前缀已生效', { tone: 'success' })
    await loadData()
  } catch (error) {
    toast.show('链接前缀保存失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    settingsSaving.value = false
  }
}

function openCreate(): void {
  Object.assign(form, { id: '', name: '创作者邀请', max_registrations: 1, initial_credits: '0.00', enabled: true })
  dialog.value = 'form'
}

function openEdit(invitation: Invitation): void {
  Object.assign(form, {
    id: invitation.id,
    name: invitation.name,
    max_registrations: invitation.max_registrations,
    initial_credits: invitation.initial_credits,
    enabled: invitation.enabled,
  })
  dialog.value = 'form'
}

async function saveInvitation(): Promise<void> {
  saving.value = true
  try {
    const path = form.id ? `/admin/invitations/${form.id}` : '/admin/invitations'
    await api<Invitation>(path, {
      method: form.id ? 'PATCH' : 'POST',
      body: JSON.stringify({
        name: form.name,
        max_registrations: Number(form.max_registrations),
        initial_credits: form.initial_credits || '0',
        enabled: form.enabled,
      }),
    })
    toast.show(form.id ? '邀请码已更新' : '邀请码已生成', { tone: 'success' })
    dialog.value = null
    await loadData()
  } catch (error) {
    toast.show('邀请码保存失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    saving.value = false
  }
}

async function toggleInvitation(invitation: Invitation): Promise<void> {
  actionId.value = invitation.id
  try {
    await api(`/admin/invitations/${invitation.id}`, {
      method: 'PATCH', body: JSON.stringify({ enabled: !invitation.enabled }),
    })
    toast.show(invitation.enabled ? '邀请码已停用' : '邀请码已启用', { tone: 'success' })
    await loadData()
  } catch (error) {
    toast.show('状态修改失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    actionId.value = null
  }
}

async function confirmDelete(): Promise<void> {
  if (!selected.value) return
  actionId.value = selected.value.id
  try {
    await api(`/admin/invitations/${selected.value.id}`, { method: 'DELETE' })
    toast.show('邀请码已删除', { message: '对应邀请链接已立即失效', tone: 'success' })
    dialog.value = null
    await loadData()
  } catch (error) {
    toast.show('邀请码删除失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    actionId.value = null
  }
}

async function copyText(value: string | null, label = '邀请链接'): Promise<void> {
  if (!value) {
    toast.show('请先配置邀请链接前缀', { tone: 'info' })
    return
  }
  try {
    await navigator.clipboard.writeText(value)
    toast.show(`${label}已复制`, { tone: 'success' })
  } catch {
    toast.show('复制失败', { message: '浏览器未授予剪贴板权限', tone: 'error' })
  }
}

async function openQr(invitation: Invitation): Promise<void> {
  if (!invitation.invite_url) {
    toast.show('请先配置邀请链接前缀', { tone: 'info' })
    return
  }
  selected.value = invitation
  qrDataUrl.value = await QRCode.toDataURL(invitation.invite_url, {
    width: 640,
    margin: 2,
    errorCorrectionLevel: 'H',
    color: { dark: '#13231d', light: '#f7faf8' },
  })
  dialog.value = 'qr'
}

function downloadQr(): void {
  if (!selected.value || !qrDataUrl.value) return
  const anchor = document.createElement('a')
  anchor.href = qrDataUrl.value
  anchor.download = `${selected.value.name}-邀请二维码.png`
  anchor.click()
}

async function copyQr(): Promise<void> {
  if (!qrDataUrl.value) return
  try {
    const blob = await (await fetch(qrDataUrl.value)).blob()
    if (!navigator.clipboard?.write || typeof ClipboardItem === 'undefined') throw new Error('unsupported')
    await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })])
    toast.show('邀请二维码已复制', { tone: 'success' })
  } catch {
    downloadQr()
    toast.show('当前浏览器不支持复制图片', { message: '已为你下载二维码 PNG', tone: 'info' })
  }
}

onMounted(() => void loadData())
</script>

<template>
  <section class="invite-admin">
    <header class="section-heading invite-admin__heading">
      <div><h2>邀请注册</h2><p>通过受控名额邀请新创作者，并为每个入口配置独立开户积分</p></div>
      <button class="button button--primary" type="button" @click="openCreate"><Plus :size="17" />生成邀请码</button>
    </header>

    <section class="invite-origin">
      <span class="invite-origin__icon"><Link2 :size="20" /></span>
      <div class="invite-origin__copy"><strong>邀请链接网址前缀</strong><small>邀请码生成后会自动拼接为“前缀 / invite / 邀请码”</small></div>
      <label><ExternalLink :size="16" /><input v-model.trim="urlPrefix" type="url" placeholder="https://studio.example.com" /></label>
      <button class="button button--secondary" type="button" :disabled="settingsSaving || !settingsDirty" @click="saveSettings">
        <LoaderCircle v-if="settingsSaving" class="spin" :size="16" /><Save v-else :size="16" />保存并生效
      </button>
    </section>

    <div class="invite-metrics" aria-label="邀请注册概览">
      <article><span><MailPlus :size="19" /></span><div><small>邀请码</small><strong class="tabular-nums">{{ invitations.length }}</strong></div></article>
      <article><span><ShieldCheck :size="19" /></span><div><small>正在启用</small><strong class="tabular-nums">{{ enabledCount }}</strong></div></article>
      <article><span><UserCheck :size="19" /></span><div><small>已注册</small><strong class="tabular-nums">{{ registeredCount }}</strong></div></article>
      <article><span><UsersRound :size="19" /></span><div><small>剩余名额</small><strong class="tabular-nums">{{ remainingCount }}</strong></div></article>
    </div>

    <div v-if="loading" class="invite-loading"><LoaderCircle class="spin" :size="24" />正在读取邀请码</div>
    <div v-else-if="invitations.length" class="invite-grid">
      <article v-for="(invitation, index) in invitations" :key="invitation.id" v-motion="{ preset: 'card', index }" class="invite-card" :data-enabled="invitation.enabled">
        <header>
          <span class="invite-card__mark"><Gift :size="20" /></span>
          <div><strong>{{ invitation.name }}</strong><small>{{ formatTime(invitation.created_at) }}</small></div>
          <button class="invite-status" type="button" :disabled="actionId === invitation.id" :aria-pressed="invitation.enabled" @click="toggleInvitation(invitation)">
            <LoaderCircle v-if="actionId === invitation.id" class="spin" :size="13" />
            <Check v-else-if="invitation.enabled" :size="13" />
            <CircleOff v-else :size="13" />{{ invitation.enabled ? '启用中' : '已停用' }}
          </button>
        </header>
        <button class="invite-code" type="button" title="复制邀请码" @click="copyText(invitation.code, '邀请码')"><code>{{ invitation.code }}</code><Copy :size="15" /></button>
        <div class="invite-progress">
          <div><span>注册进度</span><strong class="tabular-nums">{{ invitation.registration_count }} / {{ invitation.max_registrations }}</strong></div>
          <span><i :style="{ width: `${Math.min(100, invitation.registration_count / invitation.max_registrations * 100)}%` }"></i></span>
        </div>
        <div class="invite-card__meta"><span><Coins :size="15" />初始积分 <strong class="tabular-nums">{{ formatCredits(invitation.initial_credits) }}</strong></span><span><UsersRound :size="15" />剩余 <strong class="tabular-nums">{{ invitation.remaining_registrations }}</strong></span></div>
        <footer>
          <button type="button" :disabled="!invitation.invite_url" @click="copyText(invitation.invite_url)"><Clipboard :size="15" />复制链接</button>
          <button type="button" :disabled="!invitation.invite_url" @click="openQr(invitation)"><QrCode :size="15" />二维码</button>
          <button type="button" @click="openEdit(invitation)"><Pencil :size="15" />编辑</button>
          <button class="invite-card__delete" type="button" @click="selected = invitation; dialog = 'delete'"><Trash2 :size="15" /><span class="sr-only">删除</span></button>
        </footer>
      </article>
    </div>
    <div v-else class="invite-empty"><span><MailPlus :size="28" /></span><strong>还没有邀请码</strong><p>生成第一个邀请码，设置名额和初始积分后即可分享。</p><button class="button button--primary" type="button" @click="openCreate"><Plus :size="16" />生成邀请码</button></div>

    <BaseDialog :open="dialog === 'form'" :title="form.id ? '编辑邀请码' : '生成邀请码'" @update:open="!$event && !saving && (dialog = null)">
      <form id="invitation-form" class="invite-form" @submit.prevent="saveInvitation">
        <div class="invite-form__hero"><span><Gift :size="23" /></span><div><strong>{{ form.id ? '调整邀请规则' : '创建一条专属邀请入口' }}</strong><p>已完成注册的积分不会因后续编辑而改变。</p></div></div>
        <label class="field"><span>邀请名称</span><input v-model.trim="form.name" required maxlength="80" placeholder="例如：首批内测创作者" /></label>
        <div class="invite-form__grid">
          <label class="field"><span>注册人数上限</span><div class="invite-number"><UsersRound :size="16" /><input v-model.number="form.max_registrations" required type="number" min="1" max="100000" step="1" /><small>人</small></div></label>
          <label class="field"><span>每人初始积分</span><div class="invite-number"><Coins :size="16" /><input v-model="form.initial_credits" required type="number" min="0" max="999999999999.99" step="0.01" /><small>积分</small></div></label>
        </div>
        <button class="invite-enabled-toggle" type="button" :aria-pressed="form.enabled" @click="form.enabled = !form.enabled"><span><i></i></span><div><strong>创建后立即启用</strong><small>停用状态下，邀请页面不能注册</small></div></button>
      </form>
      <template #footer><button class="button button--ghost" type="button" :disabled="saving" @click="dialog = null">取消</button><button class="button button--primary" type="submit" form="invitation-form" :disabled="saving"><LoaderCircle v-if="saving" class="spin" :size="17" /><Check v-else :size="17" />{{ form.id ? '保存修改' : '生成邀请码' }}</button></template>
    </BaseDialog>

    <BaseDialog :open="dialog === 'qr'" title="邀请二维码" @update:open="!$event && (dialog = null)">
      <div class="invite-qr"><div><img :src="qrDataUrl" alt="邀请注册链接二维码" /></div><strong>{{ selected?.name }}</strong><p>{{ selected?.invite_url }}</p></div>
      <template #footer><button class="button button--ghost" type="button" @click="downloadQr"><QrCode :size="16" />下载 PNG</button><button class="button button--primary" type="button" @click="copyQr"><Copy :size="16" />复制二维码</button></template>
    </BaseDialog>

    <BaseDialog :open="dialog === 'delete'" title="删除邀请码" @update:open="!$event && !actionId && (dialog = null)">
      <div class="invite-delete"><span><Trash2 :size="22" /></span><div><strong>删除“{{ selected?.name }}”</strong><p>删除后邀请链接立即失效；历史注册记录和积分流水会继续保留。</p></div></div>
      <template #footer><button class="button button--ghost" type="button" :disabled="Boolean(actionId)" @click="dialog = null">取消</button><button class="button button--danger" type="button" :disabled="Boolean(actionId)" @click="confirmDelete"><LoaderCircle v-if="actionId" class="spin" :size="16" /><Trash2 v-else :size="16" />确认删除</button></template>
    </BaseDialog>
  </section>
</template>

<style scoped>
.invite-admin { display: grid; min-width: 0; gap: 18px; color: var(--ink); }
.invite-admin__heading { align-items: center; }
.invite-origin { display: grid; grid-template-columns: 44px minmax(190px, .8fr) minmax(260px, 1.3fr) auto; align-items: center; gap: 14px; padding: 14px; border-radius: 8px; background: var(--surface); box-shadow: var(--shadow-border), 0 12px 32px rgb(0 0 0 / 7%); }
.invite-origin__icon, .invite-form__hero>span { display: inline-flex; width: 44px; height: 44px; align-items: center; justify-content: center; border-radius: 7px; color: var(--brand-strong); background: var(--brand-soft); }
.invite-origin__copy strong, .invite-origin__copy small { display: block; }
.invite-origin__copy small { margin-top: 3px; color: var(--ink-tertiary); font-size: 10px; text-wrap: pretty; }
.invite-origin label { display: flex; min-width: 0; min-height: 42px; align-items: center; gap: 9px; padding: 0 12px; border-radius: 6px; color: var(--ink-tertiary); background: var(--surface-strong); box-shadow: inset 0 0 0 1px var(--line); transition-property: box-shadow, background-color; transition-duration: var(--duration-fast); }
.invite-origin label:focus-within { color: var(--brand); background: var(--surface); box-shadow: inset 0 0 0 1px var(--brand), 0 0 0 3px var(--brand-soft); }
.invite-origin input { min-width: 0; flex: 1; border: 0; outline: 0; color: var(--ink); background: transparent; }
.invite-metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
.invite-metrics article { display: grid; min-height: 82px; grid-template-columns: 38px minmax(0, 1fr); align-items: center; gap: 10px; padding: 12px; border-radius: 8px; background: var(--surface); box-shadow: var(--shadow-border); }
.invite-metrics article>span { display: inline-flex; width: 38px; height: 38px; align-items: center; justify-content: center; border-radius: 7px; color: var(--brand); background: var(--brand-soft); }
.invite-metrics small, .invite-metrics strong { display: block; }
.invite-metrics small { color: var(--ink-tertiary); font-size: 10px; }
.invite-metrics strong { margin-top: 2px; font-size: 21px; }
.invite-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.invite-card { display: grid; min-width: 0; gap: 14px; padding: 15px; border-radius: 8px; background: var(--surface); box-shadow: var(--shadow-border), 0 12px 28px rgb(0 0 0 / 6%); transition-property: transform, box-shadow, opacity; transition-duration: var(--duration-fast); }
.invite-card:hover { transform: translateY(-2px); box-shadow: var(--shadow-hover); }
.invite-card[data-enabled='false'] { opacity: .72; }
.invite-card header { display: grid; grid-template-columns: 38px minmax(0, 1fr) auto; align-items: center; gap: 10px; }
.invite-card__mark { display: inline-flex; width: 38px; height: 38px; align-items: center; justify-content: center; border-radius: 7px; color: var(--brand); background: var(--brand-soft); }
.invite-card header strong, .invite-card header small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.invite-card header small { margin-top: 3px; color: var(--ink-tertiary); font-size: 9px; }
.invite-status { display: inline-flex; min-height: 30px; align-items: center; gap: 5px; padding: 0 9px; border: 0; border-radius: 15px; color: var(--ink-tertiary); background: var(--surface-strong); font-size: 10px; font-weight: 700; }
.invite-status[aria-pressed='true'] { color: var(--success); background: color-mix(in srgb, var(--success) 12%, transparent); }
.invite-code { display: flex; min-width: 0; min-height: 38px; align-items: center; justify-content: space-between; gap: 8px; padding: 0 10px; border: 0; border-radius: 6px; color: var(--ink-secondary); background: var(--surface-strong); box-shadow: inset 0 0 0 1px var(--line); }
.invite-code code { overflow: hidden; text-overflow: ellipsis; font-size: 10px; white-space: nowrap; }
.invite-progress { display: grid; gap: 7px; }
.invite-progress>div { display: flex; justify-content: space-between; color: var(--ink-tertiary); font-size: 10px; }
.invite-progress>div strong { color: var(--ink-secondary); }
.invite-progress>span { overflow: hidden; height: 5px; border-radius: 3px; background: var(--surface-strong); }
.invite-progress i { display: block; height: 100%; border-radius: inherit; background: var(--brand); transition-property: width; transition-duration: var(--duration-medium); }
.invite-card__meta { display: flex; justify-content: space-between; gap: 8px; color: var(--ink-tertiary); font-size: 10px; }
.invite-card__meta span { display: inline-flex; align-items: center; gap: 5px; }
.invite-card__meta strong { color: var(--ink); }
.invite-card footer { display: grid; grid-template-columns: 1fr 1fr 1fr 40px; gap: 5px; }
.invite-card footer button { display: inline-flex; min-width: 0; min-height: 40px; align-items: center; justify-content: center; gap: 5px; border: 0; border-radius: 6px; color: var(--ink-secondary); background: var(--surface-strong); font-size: 10px; font-weight: 650; transition-property: color, background-color, scale; transition-duration: var(--duration-fast); }
.invite-card footer button:hover:not(:disabled) { color: var(--ink); background: var(--brand-soft); }
.invite-card footer button:active:not(:disabled) { scale: .96; }
.invite-card footer button:disabled { cursor: not-allowed; opacity: .42; }
.invite-card footer .invite-card__delete { color: var(--danger); }
.invite-loading, .invite-empty { display: flex; min-height: 220px; align-items: center; justify-content: center; gap: 9px; color: var(--ink-tertiary); }
.invite-empty { flex-direction: column; text-align: center; }
.invite-empty>span { display: inline-flex; width: 56px; height: 56px; align-items: center; justify-content: center; border-radius: 8px; color: var(--brand); background: var(--brand-soft); }
.invite-empty strong { color: var(--ink); font-size: 16px; }
.invite-empty p { margin: -3px 0 8px; font-size: 11px; }
.invite-form { display: grid; gap: 15px; }
.invite-form__hero { display: grid; grid-template-columns: 44px minmax(0, 1fr); align-items: center; gap: 12px; padding: 12px; border-radius: 8px; background: var(--surface-strong); }
.invite-form__hero strong, .invite-form__hero p { display: block; margin: 0; }
.invite-form__hero p { margin-top: 3px; color: var(--ink-tertiary); font-size: 10px; }
.invite-form__grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.invite-number { display: flex; min-height: 42px; align-items: center; gap: 8px; padding: 0 11px; border-radius: 6px; background: var(--surface-strong); box-shadow: inset 0 0 0 1px var(--line); }
.invite-number input { min-width: 0; flex: 1; border: 0; outline: 0; color: var(--ink); background: transparent; }
.invite-number small { color: var(--ink-tertiary); }
.invite-enabled-toggle { display: grid; min-height: 58px; grid-template-columns: 40px minmax(0, 1fr); align-items: center; gap: 11px; padding: 9px 11px; border: 0; border-radius: 7px; color: var(--ink); background: var(--surface-strong); text-align: left; }
.invite-enabled-toggle>span { position: relative; width: 36px; height: 20px; border-radius: 10px; background: var(--line-strong); }
.invite-enabled-toggle>span i { position: absolute; top: 3px; left: 3px; width: 14px; height: 14px; border-radius: 50%; background: #fff; transition-property: transform, background-color; transition-duration: var(--duration-fast); }
.invite-enabled-toggle[aria-pressed='true']>span { background: var(--brand); }
.invite-enabled-toggle[aria-pressed='true']>span i { transform: translateX(16px); }
.invite-enabled-toggle strong, .invite-enabled-toggle small { display: block; }
.invite-enabled-toggle small { margin-top: 2px; color: var(--ink-tertiary); font-size: 10px; }
.invite-qr { display: grid; justify-items: center; gap: 8px; text-align: center; }
.invite-qr>div { padding: 12px; border-radius: 8px; background: #f7faf8; box-shadow: 0 18px 44px rgb(0 0 0 / 18%); }
.invite-qr img { display: block; width: min(260px, 70vw); aspect-ratio: 1; outline: 1px solid rgb(0 0 0 / 10%); }
.invite-qr p { max-width: 440px; margin: 0; overflow-wrap: anywhere; color: var(--ink-tertiary); font-size: 10px; }
.invite-delete { display: grid; grid-template-columns: 46px minmax(0, 1fr); align-items: center; gap: 12px; }
.invite-delete>span { display: inline-flex; width: 46px; height: 46px; align-items: center; justify-content: center; border-radius: 7px; color: var(--danger); background: color-mix(in srgb, var(--danger) 12%, transparent); }
.invite-delete p { margin: 5px 0 0; color: var(--ink-tertiary); font-size: 11px; text-wrap: pretty; }
@media (max-width: 1160px) { .invite-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } .invite-origin { grid-template-columns: 44px minmax(0, 1fr) auto; } .invite-origin label { grid-column: 2 / -1; grid-row: 2; } }
@media (max-width: 720px) { .invite-admin { gap: 12px; } .invite-admin__heading { align-items: stretch; } .invite-admin__heading .button { width: 100%; } .invite-origin { grid-template-columns: 40px minmax(0, 1fr); gap: 10px; padding: 11px; } .invite-origin__icon { width: 40px; height: 40px; } .invite-origin label, .invite-origin>.button { grid-column: 1 / -1; grid-row: auto; width: 100%; } .invite-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 7px; } .invite-metrics article { min-height: 70px; padding: 9px; } .invite-grid { grid-template-columns: 1fr; gap: 9px; } .invite-card { padding: 12px; } .invite-form__grid { grid-template-columns: 1fr; } }
@media (prefers-reduced-motion: reduce) { .invite-card, .invite-progress i, .invite-enabled-toggle>span i, .invite-card footer button { transition: none !important; } }
</style>
