<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import {
  BadgeCheck,
  Ban,
  CalendarClock,
  Check,
  CircleDollarSign,
  CircleMinus,
  CirclePlus,
  Coins,
  History,
  KeyRound,
  LoaderCircle,
  Mail,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  ShieldCheck,
  UserPlus,
  UserRound,
  UsersRound,
  WalletCards,
} from 'lucide-vue-next'

import BaseDialog from '@/components/BaseDialog.vue'
import UiSelect from '@/components/UiSelect.vue'
import { api } from '@/lib/api'
import { useAuthStore } from '@/stores/auth'
import { useToastStore } from '@/stores/toast'
import type {
  AdminCreditAdjustmentResult,
  AdminUser,
  AdminUserPage,
  CreditLedgerEntry,
  CreditLedgerPage,
  UserRole,
} from '@/types'

type UserDialog = 'user' | 'credit' | 'password' | 'ledger' | null

const auth = useAuthStore()
const toast = useToastStore()
const users = ref<AdminUser[]>([])
const total = ref(0)
const activeCount = ref(0)
const adminCount = ref(0)
const totalBalance = ref('0')
const loading = ref(true)
const loadingMore = ref(false)
const saving = ref(false)
const search = ref('')
const roleFilter = ref('all')
const statusFilter = ref('all')
const dialog = ref<UserDialog>(null)
const selectedUser = ref<AdminUser | null>(null)
const ledger = ref<CreditLedgerEntry[]>([])
const ledgerNextBefore = ref<string | null>(null)
const ledgerNextBeforeId = ref<string | null>(null)
const ledgerLoading = ref(false)
const ledgerLoadingMore = ref(false)
let searchTimer: ReturnType<typeof setTimeout> | null = null

const userForm = reactive({
  id: '',
  email: '',
  display_name: '',
  password: '',
  role: 'user' as UserRole,
  is_active: true,
  initial_credits: '0.00',
})
const creditForm = reactive({ mode: 'grant' as 'grant' | 'deduct', amount: '', reason: '' })
const passwordForm = reactive({ password: '', confirm: '' })

const roleOptions = [
  { value: 'all', label: '全部角色', description: '管理员和创作者', icon: UsersRound },
  { value: 'admin', label: '管理员', description: '可进入管理后台', icon: ShieldCheck },
  { value: 'user', label: '创作者', description: '仅使用创作功能', icon: UserRound },
]
const statusOptions = [
  { value: 'all', label: '全部状态', description: '显示所有账号', icon: UsersRound },
  { value: 'active', label: '正常使用', description: '可以登录与提交任务', icon: BadgeCheck },
  { value: 'inactive', label: '已停用', description: '禁止登录与刷新会话', icon: Ban },
]
const roleEditOptions = roleOptions.filter((item) => item.value !== 'all')
const isEditingSelf = computed(() => userForm.id === auth.session?.user.id)
const hasMore = computed(() => users.value.length < total.value)
const dialogTitle = computed(() => {
  if (dialog.value === 'credit') return `调整 ${selectedUser.value?.display_name ?? ''} 的积分`
  if (dialog.value === 'password') return `重置 ${selectedUser.value?.display_name ?? ''} 的密码`
  if (dialog.value === 'ledger') return `${selectedUser.value?.display_name ?? ''} 的积分流水`
  return userForm.id ? '编辑用户' : '新建用户'
})

function formatCredits(value: string | number): string {
  return Number(value).toLocaleString('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
}

function formatTime(value: string | null): string {
  if (!value) return '暂无任务'
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(new Date(value))
}

function ledgerSource(entry: CreditLedgerEntry): string {
  if (entry.reference_type === 'admin_adjustment') return '管理员调整'
  if (entry.reference_type === 'ai_task') return 'AI 任务'
  return '系统记账'
}

function buildUserQuery(offset: number): string {
  const params = new URLSearchParams({ offset: String(offset), limit: '50' })
  if (search.value.trim()) params.set('search', search.value.trim())
  if (roleFilter.value !== 'all') params.set('role', roleFilter.value)
  if (statusFilter.value !== 'all') params.set('is_active', String(statusFilter.value === 'active'))
  return `/admin/users?${params.toString()}`
}

async function loadUsers(reset = true): Promise<void> {
  if (reset) loading.value = true
  else loadingMore.value = true
  try {
    const page = await api<AdminUserPage>(buildUserQuery(reset ? 0 : users.value.length))
    users.value = reset ? page.items : [...users.value, ...page.items]
    total.value = page.total
    activeCount.value = page.active_count
    adminCount.value = page.admin_count
    totalBalance.value = page.total_balance
  } catch (error) {
    toast.show('用户数据加载失败', {
      message: error instanceof Error ? error.message : undefined,
      tone: 'error',
    })
  } finally {
    loading.value = false
    loadingMore.value = false
  }
}

function scheduleReload(): void {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => void loadUsers(true), 260)
}

watch(search, scheduleReload)
watch([roleFilter, statusFilter], () => void loadUsers(true))
onMounted(() => void loadUsers(true))
onBeforeUnmount(() => searchTimer && clearTimeout(searchTimer))

function openUser(user?: AdminUser): void {
  selectedUser.value = user ?? null
  Object.assign(userForm, {
    id: user?.id ?? '',
    email: user?.email ?? '',
    display_name: user?.display_name ?? '',
    password: '',
    role: user?.role ?? 'user',
    is_active: user?.is_active ?? true,
    initial_credits: '0.00',
  })
  dialog.value = 'user'
}

function openCredit(user: AdminUser): void {
  selectedUser.value = user
  Object.assign(creditForm, { mode: 'grant', amount: '', reason: '' })
  dialog.value = 'credit'
}

function openPassword(user: AdminUser): void {
  selectedUser.value = user
  Object.assign(passwordForm, { password: '', confirm: '' })
  dialog.value = 'password'
}

async function openLedger(user: AdminUser): Promise<void> {
  selectedUser.value = user
  ledger.value = []
  ledgerNextBefore.value = null
  ledgerNextBeforeId.value = null
  dialog.value = 'ledger'
  await loadLedger(true)
}

function closeDialog(): void {
  if (saving.value) return
  dialog.value = null
  selectedUser.value = null
}

async function saveUser(): Promise<void> {
  saving.value = true
  try {
    const payload = userForm.id
      ? {
          display_name: userForm.display_name,
          role: userForm.role,
          is_active: userForm.is_active,
        }
      : {
          email: userForm.email,
          display_name: userForm.display_name,
          password: userForm.password,
          role: userForm.role,
          initial_credits: userForm.initial_credits || '0',
        }
    const path = userForm.id ? `/admin/users/${userForm.id}` : '/admin/users'
    await api<AdminUser>(path, {
      method: userForm.id ? 'PATCH' : 'POST',
      body: JSON.stringify(payload),
    })
    if (isEditingSelf.value) await auth.refreshSession()
    toast.show(userForm.id ? '用户资料已更新' : '用户已创建', { tone: 'success' })
    closeDialog()
    await loadUsers(true)
  } catch (error) {
    toast.show('用户保存失败', {
      message: error instanceof Error ? error.message : undefined,
      tone: 'error',
    })
  } finally {
    saving.value = false
  }
}

async function adjustCredits(): Promise<void> {
  if (!selectedUser.value) return
  const rawAmount = Number(creditForm.amount)
  if (!Number.isFinite(rawAmount) || rawAmount <= 0) {
    toast.show('请输入大于 0 的积分数量', { tone: 'error' })
    return
  }
  saving.value = true
  try {
    const amount = creditForm.mode === 'deduct' ? -rawAmount : rawAmount
    const result = await api<AdminCreditAdjustmentResult>(
      `/admin/users/${selectedUser.value.id}/credits/adjust`,
      {
        method: 'POST',
        body: JSON.stringify({ amount: amount.toFixed(2), reason: creditForm.reason }),
      },
    )
    const index = users.value.findIndex((item) => item.id === result.user.id)
    if (index >= 0) users.value[index] = result.user
    if (result.user.id === auth.session?.user.id) await auth.refreshSession()
    toast.show(creditForm.mode === 'grant' ? '积分已发放' : '积分已扣减', {
      message: `当前余额 ${formatCredits(result.user.credit_balance)}`,
      tone: 'success',
    })
    closeDialog()
    await loadUsers(true)
  } catch (error) {
    toast.show('积分调整失败', {
      message: error instanceof Error ? error.message : undefined,
      tone: 'error',
    })
  } finally {
    saving.value = false
  }
}

async function resetPassword(): Promise<void> {
  if (!selectedUser.value) return
  if (passwordForm.password !== passwordForm.confirm) {
    toast.show('两次输入的密码不一致', { tone: 'error' })
    return
  }
  saving.value = true
  try {
    await api(`/admin/users/${selectedUser.value.id}/reset-password`, {
      method: 'POST',
      body: JSON.stringify({ password: passwordForm.password }),
    })
    toast.show('密码已重置', {
      message: '该用户已有刷新会话已失效，需要使用新密码重新登录',
      tone: 'success',
    })
    closeDialog()
  } catch (error) {
    toast.show('密码重置失败', {
      message: error instanceof Error ? error.message : undefined,
      tone: 'error',
    })
  } finally {
    saving.value = false
  }
}

async function loadLedger(reset = false): Promise<void> {
  if (!selectedUser.value) return
  if (reset) ledgerLoading.value = true
  else ledgerLoadingMore.value = true
  try {
    const params = new URLSearchParams({ limit: '30' })
    if (!reset && ledgerNextBefore.value) params.set('before', ledgerNextBefore.value)
    if (!reset && ledgerNextBeforeId.value) params.set('before_id', ledgerNextBeforeId.value)
    const page = await api<CreditLedgerPage>(
      `/admin/users/${selectedUser.value.id}/credit-ledger?${params.toString()}`,
    )
    ledger.value = reset ? page.items : [...ledger.value, ...page.items]
    ledgerNextBefore.value = page.next_before
    ledgerNextBeforeId.value = page.next_before_id
  } catch (error) {
    toast.show('积分流水加载失败', {
      message: error instanceof Error ? error.message : undefined,
      tone: 'error',
    })
  } finally {
    ledgerLoading.value = false
    ledgerLoadingMore.value = false
  }
}
</script>

<template>
  <section class="admin-users-section">
    <header class="section-heading admin-users-heading">
      <div><h2>用户与积分</h2><p>管理当前租户账号、访问状态与积分资产</p></div>
      <button class="button button--primary" type="button" @click="openUser()"><UserPlus :size="17" />新建用户</button>
    </header>

    <div class="user-metrics" aria-label="用户概览">
      <article v-motion="{ preset: 'card', index: 0 }"><span><UsersRound :size="19" /></span><div><small>筛选结果</small><strong class="tabular-nums">{{ total }}</strong></div></article>
      <article v-motion="{ preset: 'card', index: 1 }"><span><BadgeCheck :size="19" /></span><div><small>有效账号</small><strong class="tabular-nums">{{ activeCount }}</strong></div></article>
      <article v-motion="{ preset: 'card', index: 2 }"><span><ShieldCheck :size="19" /></span><div><small>管理员</small><strong class="tabular-nums">{{ adminCount }}</strong></div></article>
      <article v-motion="{ preset: 'card', index: 3 }"><span><WalletCards :size="19" /></span><div><small>租户积分总额</small><strong class="tabular-nums">{{ formatCredits(totalBalance) }}</strong></div></article>
    </div>

    <div class="user-toolbar" aria-label="用户筛选">
      <label class="user-search"><Search :size="17" /><input v-model="search" type="search" placeholder="搜索姓名或邮箱" aria-label="搜索用户" /></label>
      <UiSelect v-model="roleFilter" :options="roleOptions" placeholder="筛选角色" variant="compact" />
      <UiSelect v-model="statusFilter" :options="statusOptions" placeholder="筛选状态" variant="compact" />
      <button class="icon-button" type="button" title="刷新用户" :disabled="loading" @click="loadUsers(true)"><RefreshCw :class="{ spin: loading }" :size="17" /></button>
    </div>

    <div class="user-directory">
      <header class="user-directory__labels" aria-hidden="true"><span>用户</span><span>角色与状态</span><span>业务数据</span><span>积分余额</span><span>最近活动</span><span>操作</span></header>
      <div v-if="loading" class="user-directory__loading"><LoaderCircle class="spin" :size="24" /><span>正在读取用户</span></div>
      <div v-else-if="users.length" class="user-directory__rows">
        <article v-for="(user, index) in users" :key="user.id" v-motion="{ preset: 'row', index }" class="user-row" :data-active="user.is_active">
          <div class="user-row__identity"><span>{{ user.display_name.slice(0, 1) }}</span><div><strong>{{ user.display_name }}</strong><small><Mail :size="12" />{{ user.email }}</small></div></div>
          <div class="user-row__access"><span :data-role="user.role"><ShieldCheck v-if="user.role === 'admin'" :size="13" /><UserRound v-else :size="13" />{{ user.role === 'admin' ? '管理员' : '创作者' }}</span><small :data-active="user.is_active"><Check v-if="user.is_active" :size="12" /><Ban v-else :size="12" />{{ user.is_active ? '正常' : '已停用' }}</small></div>
          <div class="user-row__usage"><span><strong class="tabular-nums">{{ user.project_count }}</strong> 项目</span><span><strong class="tabular-nums">{{ user.task_count }}</strong> 任务</span></div>
          <button class="user-row__balance" type="button" title="调整积分" @click="openCredit(user)"><Coins :size="16" /><span><strong class="tabular-nums">{{ formatCredits(user.credit_balance) }}</strong><small>调整积分</small></span></button>
          <div class="user-row__activity"><CalendarClock :size="15" /><span><strong>{{ formatTime(user.last_task_at) }}</strong><small>加入于 {{ formatTime(user.created_at) }}</small></span></div>
          <div class="user-row__actions">
            <button class="icon-button" type="button" title="积分流水" @click="openLedger(user)"><History :size="16" /></button>
            <button class="icon-button" type="button" title="重置密码" @click="openPassword(user)"><KeyRound :size="16" /></button>
            <button class="icon-button" type="button" title="编辑用户" @click="openUser(user)"><Pencil :size="16" /></button>
          </div>
        </article>
      </div>
      <div v-else class="user-directory__empty"><span><UsersRound :size="26" /></span><strong>没有匹配的用户</strong><p>调整搜索条件，或新建一个创作者账号。</p><button class="button button--primary" type="button" @click="openUser()"><Plus :size="16" />新建用户</button></div>
      <button v-if="hasMore" class="user-load-more" type="button" :disabled="loadingMore" @click="loadUsers(false)"><LoaderCircle v-if="loadingMore" class="spin" :size="16" /><Plus v-else :size="16" />加载更多用户</button>
    </div>

    <BaseDialog :open="Boolean(dialog)" :title="dialogTitle" :description="dialog === 'ledger' ? '查看每次积分变动及调整后余额' : undefined" :wide="dialog === 'ledger'" @update:open="(open) => !open && closeDialog()">
      <form v-if="dialog === 'user'" id="admin-user-form" class="admin-user-form" @submit.prevent="saveUser">
        <div class="user-form-identity"><span>{{ (userForm.display_name || '新').slice(0, 1) }}</span><div><strong>{{ userForm.display_name || '新用户' }}</strong><p>{{ userForm.email || '填写账号信息并分配访问角色' }}</p></div></div>
        <label class="field"><span>显示名称</span><input v-model="userForm.display_name" required maxlength="80" placeholder="用户在系统内显示的名称" /></label>
        <label class="field"><span>登录邮箱</span><input v-model="userForm.email" required type="email" maxlength="255" :disabled="Boolean(userForm.id)" placeholder="name@example.com" /></label>
        <label v-if="!userForm.id" class="field"><span>初始密码</span><input v-model="userForm.password" required type="password" minlength="8" maxlength="128" autocomplete="new-password" placeholder="至少 8 位" /></label>
        <label v-if="!userForm.id" class="field"><span>开户积分</span><div class="credit-input"><Coins :size="16" /><input v-model="userForm.initial_credits" required type="number" min="0" max="999999999999.99" step="0.01" /><small>积分</small></div></label>
        <div class="field"><span>账号角色</span><UiSelect v-model="userForm.role" :options="roleEditOptions" :disabled="isEditingSelf" placeholder="选择角色" /></div>
        <div v-if="userForm.id" class="field"><span>账号状态</span><button class="account-state-toggle" type="button" :disabled="isEditingSelf" :aria-pressed="userForm.is_active" @click="userForm.is_active = !userForm.is_active"><span><i></i></span><div><strong>{{ userForm.is_active ? '允许登录' : '禁止登录' }}</strong><small>{{ userForm.is_active ? '可使用创作与任务功能' : '刷新会话将立即失效' }}</small></div></button></div>
        <aside v-if="isEditingSelf" class="user-self-protection"><ShieldCheck :size="16" /><span>当前登录账号不能停用或移除管理员权限。</span></aside>
      </form>

      <form v-else-if="dialog === 'credit'" id="admin-credit-form" class="admin-credit-form" @submit.prevent="adjustCredits">
        <div class="credit-user-summary"><span>{{ selectedUser?.display_name.slice(0, 1) }}</span><div><strong>{{ selectedUser?.display_name }}</strong><p>{{ selectedUser?.email }}</p></div><aside><small>当前余额</small><strong class="tabular-nums">{{ formatCredits(selectedUser?.credit_balance ?? '0') }}</strong></aside></div>
        <div class="credit-mode" aria-label="积分调整方式"><button type="button" :aria-pressed="creditForm.mode === 'grant'" @click="creditForm.mode = 'grant'"><CirclePlus :size="18" /><span><strong>发放积分</strong><small>增加用户可用余额</small></span></button><button type="button" :aria-pressed="creditForm.mode === 'deduct'" @click="creditForm.mode = 'deduct'"><CircleMinus :size="18" /><span><strong>扣减积分</strong><small>余额不能低于零</small></span></button></div>
        <label class="field"><span>调整数量</span><div class="credit-amount-input" :data-mode="creditForm.mode"><CircleDollarSign :size="20" /><input v-model="creditForm.amount" required type="number" min="0.01" max="999999999999.99" step="0.01" placeholder="0.00" /><small>积分</small></div></label>
        <label class="field"><span>调整原因</span><textarea v-model="creditForm.reason" required minlength="2" maxlength="120" rows="3" placeholder="例如：活动奖励、客服补偿、人工冲正"></textarea></label>
        <aside class="credit-audit-note"><History :size="16" /><span>保存后会写入不可覆盖的积分流水，并记录调整后的余额。</span></aside>
      </form>

      <form v-else-if="dialog === 'password'" id="admin-password-form" class="admin-password-form" @submit.prevent="resetPassword">
        <div class="password-reset-hero"><span><KeyRound :size="22" /></span><div><strong>设置新的登录密码</strong><p>完成后，该用户已有刷新会话会立即失效。</p></div></div>
        <label class="field"><span>新密码</span><input v-model="passwordForm.password" required type="password" minlength="8" maxlength="128" autocomplete="new-password" placeholder="至少 8 位" /></label>
        <label class="field"><span>确认新密码</span><input v-model="passwordForm.confirm" required type="password" minlength="8" maxlength="128" autocomplete="new-password" placeholder="再次输入新密码" /></label>
      </form>

      <div v-else-if="dialog === 'ledger'" class="credit-ledger">
        <div class="ledger-balance"><span><WalletCards :size="20" /></span><div><small>当前可用积分</small><strong class="tabular-nums">{{ formatCredits(selectedUser?.credit_balance ?? '0') }}</strong></div><button class="button button--primary" type="button" @click="selectedUser && openCredit(selectedUser)"><Plus :size="16" />调整积分</button></div>
        <div v-if="ledgerLoading" class="ledger-loading"><LoaderCircle class="spin" :size="22" />正在读取流水</div>
        <div v-else-if="ledger.length" class="ledger-list">
          <article v-for="entry in ledger" :key="entry.id" :data-positive="Number(entry.amount) > 0"><span><CirclePlus v-if="Number(entry.amount) > 0" :size="17" /><CircleMinus v-else :size="17" /></span><div><strong>{{ entry.reason }}</strong><small>{{ ledgerSource(entry) }} · {{ formatTime(entry.created_at) }}</small></div><aside><strong class="tabular-nums">{{ Number(entry.amount) > 0 ? '+' : '' }}{{ formatCredits(entry.amount) }}</strong><small class="tabular-nums">余额 {{ formatCredits(entry.balance_after) }}</small></aside></article>
          <button v-if="ledgerNextBefore" class="user-load-more" type="button" :disabled="ledgerLoadingMore" @click="loadLedger(false)"><LoaderCircle v-if="ledgerLoadingMore" class="spin" :size="16" /><Plus v-else :size="16" />加载更早流水</button>
        </div>
        <div v-else class="ledger-empty"><History :size="25" /><strong>暂无积分流水</strong><p>积分发放、扣减和 AI 任务消费会显示在这里。</p></div>
      </div>

      <template v-if="dialog !== 'ledger'" #footer>
        <button class="button button--ghost" type="button" :disabled="saving" @click="closeDialog">取消</button>
        <button v-if="dialog === 'user'" class="button button--primary" type="submit" form="admin-user-form" :disabled="saving"><LoaderCircle v-if="saving" class="spin" :size="17" /><Check v-else :size="17" />{{ userForm.id ? '保存修改' : '创建用户' }}</button>
        <button v-else-if="dialog === 'credit'" class="button button--primary" type="submit" form="admin-credit-form" :disabled="saving"><LoaderCircle v-if="saving" class="spin" :size="17" /><Coins v-else :size="17" />确认调整</button>
        <button v-else-if="dialog === 'password'" class="button button--primary" type="submit" form="admin-password-form" :disabled="saving"><LoaderCircle v-if="saving" class="spin" :size="17" /><KeyRound v-else :size="17" />重置密码</button>
      </template>
      <template v-else #footer><button class="button button--ghost" type="button" @click="closeDialog">关闭</button></template>
    </BaseDialog>
  </section>
</template>
