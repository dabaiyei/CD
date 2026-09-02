import { computed, readonly, ref } from 'vue'

interface BeforeInstallPromptEvent extends Event {
  prompt: () => Promise<void>
  userChoice: Promise<{ outcome: 'accepted' | 'dismissed'; platform: string }>
}

interface NavigatorWithStandalone extends Navigator {
  standalone?: boolean
}

const deferredPrompt = ref<BeforeInstallPromptEvent | null>(null)
const installed = ref(false)
const ios = ref(false)
const mobile = ref(false)
const initialized = ref(false)
const installInstructionsOpen = ref(false)

const canInstall = computed(() => (
  !installed.value && (Boolean(deferredPrompt.value) || mobile.value)
))

export function initializePwaInstall(): void {
  if (initialized.value || typeof window === 'undefined') return
  initialized.value = true
  ios.value = /iphone|ipad|ipod/i.test(window.navigator.userAgent)
  mobile.value = window.matchMedia('(max-width: 1080px) and (pointer: coarse)').matches
    || /android|iphone|ipad|ipod/i.test(window.navigator.userAgent)
  installed.value = window.matchMedia('(display-mode: standalone)').matches
    || Boolean((window.navigator as NavigatorWithStandalone).standalone)

  window.addEventListener('beforeinstallprompt', (event) => {
    event.preventDefault()
    deferredPrompt.value = event as BeforeInstallPromptEvent
  })
  window.addEventListener('appinstalled', () => {
    installed.value = true
    deferredPrompt.value = null
    installInstructionsOpen.value = false
  })
}

export function usePwaInstall() {
  async function requestInstall(): Promise<'accepted' | 'dismissed' | 'instructions' | 'unavailable'> {
    if (installed.value) return 'unavailable'
    if (!deferredPrompt.value) {
      installInstructionsOpen.value = true
      return 'instructions'
    }

    const prompt = deferredPrompt.value
    await prompt.prompt()
    const choice = await prompt.userChoice
    if (choice.outcome === 'accepted') {
      deferredPrompt.value = null
    }
    return choice.outcome
  }

  return {
    canInstall,
    installed: readonly(installed),
    isIos: readonly(ios),
    isMobile: readonly(mobile),
    installInstructionsOpen,
    requestInstall,
  }
}
