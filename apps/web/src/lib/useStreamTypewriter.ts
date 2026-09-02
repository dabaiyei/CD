import gsap from 'gsap'
import { onUnmounted, ref, watch, type Ref } from 'vue'

import { prefersReducedMotion } from '@/lib/motion'

interface StreamTypewriterOptions {
  onFrame?: () => void
}

interface StreamTypewriter {
  displayedText: Ref<string>
  isTyping: Ref<boolean>
  finish: (maxDurationMs?: number) => Promise<void>
  reset: (value?: string) => void
}

const punctuationPause = /[。！？!?]/u
const softPunctuationPause = /[，、；：,;:\n]/u
const segmenter = typeof Intl.Segmenter === 'function'
  ? new Intl.Segmenter('zh-CN', { granularity: 'grapheme' })
  : null

function splitGraphemes(value: string): string[] {
  if (!segmenter) return Array.from(value)
  return Array.from(segmenter.segment(value), (item) => item.segment)
}

function preferredCharactersPerSecond(totalLength: number, backlog: number): number {
  const base = totalLength < 100 ? 28 : totalLength < 500 ? 40 : totalLength < 1600 ? 54 : 68
  return Math.min(260, base * (1 + Math.min(3.2, backlog / 72)))
}

export function useStreamTypewriter(
  source: Readonly<Ref<string>>,
  options: StreamTypewriterOptions = {},
): StreamTypewriter {
  const displayedText = ref('')
  const isTyping = ref(false)
  const drainWaiters = new Map<() => void, ReturnType<typeof setTimeout>>()

  let sourceText = ''
  let targetText = ''
  let targetSegments: string[] = []
  let displayedCount = 0
  let characterBudget = 0
  let nextCharacterAt = 0
  let finishDeadline = 0
  let tickerAttached = false

  function stopTicker(): void {
    if (!tickerAttached) return
    gsap.ticker.remove(renderFrame)
    tickerAttached = false
    isTyping.value = false
  }

  function startTicker(): void {
    if (tickerAttached || displayedCount >= targetSegments.length) return
    gsap.ticker.add(renderFrame)
    tickerAttached = true
    isTyping.value = true
  }

  function resolveDrainWaiters(): void {
    for (const [resolve, timer] of drainWaiters) {
      clearTimeout(timer)
      resolve()
    }
    drainWaiters.clear()
  }

  function renderAll(): void {
    displayedCount = targetSegments.length
    displayedText.value = targetText
    characterBudget = 0
    nextCharacterAt = 0
    finishDeadline = 0
    stopTicker()
    options.onFrame?.()
    resolveDrainWaiters()
  }

  function renderFrame(_time: number, deltaTime: number): void {
    const now = performance.now()
    const backlog = targetSegments.length - displayedCount
    if (backlog <= 0) {
      stopTicker()
      resolveDrainWaiters()
      return
    }
    if (now < nextCharacterAt) return

    let charactersPerSecond = preferredCharactersPerSecond(targetSegments.length, backlog)
    if (finishDeadline > now) {
      const remainingSeconds = Math.max((finishDeadline - now) / 1000, 0.05)
      charactersPerSecond = Math.max(charactersPerSecond, backlog / remainingSeconds)
    }

    characterBudget += Math.min(deltaTime, 64) * charactersPerSecond / 1000
    if (characterBudget < 1) return

    const finishing = finishDeadline > 0
    const maxBatch = finishing ? 32 : backlog > 240 ? 8 : backlog > 96 ? 4 : 2
    const requested = Math.min(Math.floor(characterBudget), backlog, maxBatch)
    let rendered = 0

    while (rendered < requested) {
      const segment = targetSegments[displayedCount + rendered]
      if (segment === undefined) break
      rendered += 1
      if (!finishing && backlog < 64) {
        if (punctuationPause.test(segment)) {
          nextCharacterAt = now + 72
          break
        }
        if (softPunctuationPause.test(segment)) {
          nextCharacterAt = now + 36
          break
        }
      }
    }

    if (!rendered) return
    const nextCount = displayedCount + rendered
    displayedText.value += targetSegments.slice(displayedCount, nextCount).join('')
    displayedCount = nextCount
    characterBudget = Math.max(0, characterBudget - rendered)
    options.onFrame?.()

    if (displayedCount >= targetSegments.length) {
      finishDeadline = 0
      stopTicker()
      resolveDrainWaiters()
    }
  }

  function syncTarget(value: string): void {
    if (value === sourceText) return

    if (value.startsWith(sourceText)) {
      targetSegments.push(...splitGraphemes(value.slice(sourceText.length)))
    } else {
      targetSegments = splitGraphemes(value)
      if (value.startsWith(displayedText.value)) {
        displayedCount = splitGraphemes(displayedText.value).length
      } else {
        displayedText.value = ''
        displayedCount = 0
      }
    }

    sourceText = value
    targetText = value
    if (prefersReducedMotion()) renderAll()
    else startTicker()
  }

  function reset(value = ''): void {
    stopTicker()
    resolveDrainWaiters()
    sourceText = ''
    targetText = ''
    targetSegments = []
    displayedCount = 0
    displayedText.value = ''
    characterBudget = 0
    nextCharacterAt = 0
    finishDeadline = 0
    if (value) syncTarget(value)
  }

  function finish(maxDurationMs = 1200): Promise<void> {
    if (displayedCount >= targetSegments.length) return Promise.resolve()
    if (prefersReducedMotion()) {
      renderAll()
      return Promise.resolve()
    }

    const backlog = targetSegments.length - displayedCount
    const duration = Math.min(maxDurationMs, Math.max(240, backlog * 10))
    finishDeadline = performance.now() + duration
    nextCharacterAt = 0
    startTicker()

    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        if (!drainWaiters.delete(resolve)) return
        renderAll()
        resolve()
      }, duration + 100)
      drainWaiters.set(resolve, timer)
    })
  }

  watch(source, syncTarget, { flush: 'sync', immediate: true })

  onUnmounted(() => {
    stopTicker()
    resolveDrainWaiters()
  })

  return { displayedText, isTyping, finish, reset }
}
