<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Download, MoreVertical, PlusSquare, Share, Smartphone, X } from 'lucide-vue-next'

import BaseDialog from '@/components/BaseDialog.vue'
import { usePwaInstall } from '@/lib/pwa'

const DISMISS_KEY = 'cineforge:pwa-install-dismissed-at'
const DISMISS_DURATION = 7 * 24 * 60 * 60 * 1000

const pwa = usePwaInstall()
const ready = ref(false)
const locallyDismissed = ref(false)
const showSuggestion = computed(() => ready.value && pwa.canInstall.value && !locallyDismissed.value)

onMounted(() => {
  const dismissedAt = Number(localStorage.getItem(DISMISS_KEY) || 0)
  locallyDismissed.value = Date.now() - dismissedAt < DISMISS_DURATION
  window.setTimeout(() => { ready.value = true }, 1200)
})

function dismiss(): void {
  locallyDismissed.value = true
  localStorage.setItem(DISMISS_KEY, String(Date.now()))
}

async function install(): Promise<void> {
  const result = await pwa.requestInstall()
  if (result === 'accepted') locallyDismissed.value = true
}
</script>

<template>
  <Transition name="pwa-install">
    <aside v-if="showSuggestion" class="pwa-install-prompt" aria-label="安装 CineForge 应用">
      <img src="/icons/app-icon-192.png" alt="" />
      <div>
        <strong>安装 CineForge</strong>
        <p>{{ pwa.isIos.value ? '添加到主屏幕，以独立应用模式打开。' : '添加到桌面，获得更沉浸的创作体验。' }}</p>
      </div>
      <button class="pwa-install-prompt__action" type="button" @click="install">
        <Download :size="16" />安装
      </button>
      <button class="pwa-install-prompt__close" type="button" title="暂不安装" aria-label="暂不安装" @click="dismiss">
        <X :size="16" />
      </button>
    </aside>
  </Transition>

  <BaseDialog
    v-model:open="pwa.installInstructionsOpen.value"
    title="添加到桌面"
    :description="pwa.isIos.value ? 'iPhone 和 iPad 需要通过 Safari 的系统菜单完成安装' : '通过浏览器菜单将 CineForge 添加到主屏幕'"
  >
    <div class="pwa-install-guide">
      <img src="/icons/app-icon-192.png" alt="CineForge 应用图标" />
      <ol>
        <li><span><Share v-if="pwa.isIos.value" :size="17" /><MoreVertical v-else :size="17" /></span><div><strong>{{ pwa.isIos.value ? '打开分享菜单' : '打开浏览器菜单' }}</strong><p>{{ pwa.isIos.value ? '点击 Safari 底部工具栏中的分享按钮。' : '点击浏览器右上角的菜单按钮。' }}</p></div></li>
        <li><span><PlusSquare :size="17" /></span><div><strong>{{ pwa.isIos.value ? '选择“添加到主屏幕”' : '选择“安装应用”' }}</strong><p>找到对应操作，并确认应用名称。</p></div></li>
        <li><span><Smartphone :size="17" /></span><div><strong>从桌面启动</strong><p>之后将以无浏览器栏的独立应用模式运行。</p></div></li>
      </ol>
    </div>
    <template #footer>
      <button class="button button--primary" type="button" @click="pwa.installInstructionsOpen.value = false">知道了</button>
    </template>
  </BaseDialog>
</template>

<style scoped>
.pwa-install-prompt { position: fixed; right: 20px; bottom: 20px; z-index: 120; display: grid; width: min(390px, calc(100vw - 28px)); min-height: 76px; grid-template-columns: 48px minmax(0, 1fr) auto 40px; align-items: center; gap: 11px; padding: 10px 8px 10px 10px; border-radius: 8px; color: var(--ink); background: color-mix(in srgb, var(--surface-strong) 92%, transparent); box-shadow: 0 22px 64px rgb(0 0 0 / 20%), var(--shadow-border); backdrop-filter: blur(18px) saturate(1.2); }
.pwa-install-prompt > img { width: 48px; height: 48px; border-radius: 6px; object-fit: cover; outline: 1px solid rgb(0 0 0 / 10%); outline-offset: -1px; }
.pwa-install-prompt > div { min-width: 0; }
.pwa-install-prompt strong { display: block; font-size: 12px; line-height: 1.35; text-wrap: balance; }
.pwa-install-prompt p { margin: 3px 0 0; color: var(--ink-secondary); font-size: 10px; line-height: 1.45; text-wrap: pretty; }
.pwa-install-prompt button { border: 0; cursor: pointer; }
.pwa-install-prompt__action { display: inline-flex; min-width: 68px; min-height: 40px; align-items: center; justify-content: center; gap: 5px; border-radius: 6px; color: #e8fff3; background: #0a6f51; font-size: 10px; font-weight: 700; box-shadow: 0 8px 20px rgb(2 55 39 / 20%); transition-property: background-color, scale; transition-duration: 160ms; }
.pwa-install-prompt__action:hover { background: #07805c; }
.pwa-install-prompt__action:active, .pwa-install-prompt__close:active { scale: .96; }
.pwa-install-prompt__close { display: inline-flex; width: 40px; height: 40px; align-items: center; justify-content: center; border-radius: 6px; color: var(--ink-tertiary); background: transparent; transition-property: color, background-color, scale; transition-duration: 150ms; }
.pwa-install-prompt__close:hover { color: var(--ink); background: var(--surface-subtle); }
.pwa-install-enter-active { transition: opacity 350ms cubic-bezier(.22, 1, .36, 1), transform 350ms cubic-bezier(.22, 1, .36, 1), filter 350ms cubic-bezier(.22, 1, .36, 1); }
.pwa-install-leave-active { transition: opacity 200ms cubic-bezier(.22, 1, .36, 1), transform 200ms cubic-bezier(.22, 1, .36, 1), filter 200ms cubic-bezier(.22, 1, .36, 1); }
.pwa-install-enter-from, .pwa-install-leave-to { opacity: 0; transform: translateY(16px) scale(.97); filter: blur(2px); }
.pwa-install-guide { display: grid; gap: 22px; padding: 5px 0; }
.pwa-install-guide > img { width: 72px; height: 72px; justify-self: center; border-radius: 10px; object-fit: cover; outline: 1px solid rgb(0 0 0 / 10%); outline-offset: -1px; box-shadow: 0 13px 28px rgb(0 0 0 / 18%); }
.pwa-install-guide ol { display: grid; gap: 8px; margin: 0; padding: 0; list-style: none; }
.pwa-install-guide li { display: grid; min-height: 62px; grid-template-columns: 40px minmax(0, 1fr); align-items: center; gap: 12px; padding: 7px 9px; border-radius: 8px; background: var(--surface-subtle); box-shadow: var(--shadow-border); }
.pwa-install-guide li > span { display: inline-flex; width: 40px; height: 40px; align-items: center; justify-content: center; border-radius: 6px; color: var(--brand-strong); background: var(--brand-soft); }
.pwa-install-guide strong { font-size: 11px; }
.pwa-install-guide p { margin: 3px 0 0; color: var(--ink-secondary); font-size: 10px; line-height: 1.5; }
:global([data-theme='dark']) .pwa-install-prompt > img, :global([data-theme='dark']) .pwa-install-guide > img { outline-color: rgb(255 255 255 / 10%); }
@media (max-width: 1080px) { .pwa-install-prompt { right: 14px; bottom: calc(72px + env(safe-area-inset-bottom)); left: 14px; width: auto; } }
@media (max-width: 430px) { .pwa-install-prompt { grid-template-columns: 44px minmax(0, 1fr) 40px; } .pwa-install-prompt > img { width: 44px; height: 44px; } .pwa-install-prompt__action { width: 40px; min-width: 40px; padding: 0; } .pwa-install-prompt__action svg { display: block; } .pwa-install-prompt__action { font-size: 0; } }
@media (prefers-reduced-motion: reduce) { .pwa-install-enter-active, .pwa-install-leave-active, .pwa-install-prompt__action, .pwa-install-prompt__close { transition: none !important; } }
</style>
