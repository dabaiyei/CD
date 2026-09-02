import gsap from 'gsap'
import type { Directive, DirectiveBinding } from 'vue'

type MotionPreset = 'section' | 'card' | 'row'

export interface MotionOptions {
  preset?: MotionPreset
  index?: number
  delay?: number
}

const activeTweens = new WeakMap<HTMLElement, gsap.core.Tween>()

const presets: Record<MotionPreset, { y: number; scale: number; duration: number }> = {
  section: { y: 12, scale: 0.995, duration: 0.5 },
  card: { y: 12, scale: 0.98, duration: 0.5 },
  row: { y: 8, scale: 0.99, duration: 0.4 },
}

function rootToken(name: string, fallback: string): string {
  if (typeof document === 'undefined') return fallback
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback
}

function durationToken(name: string, fallbackMs: number): number {
  const value = rootToken(name, `${fallbackMs}ms`)
  const amount = Number.parseFloat(value)
  if (!Number.isFinite(amount)) return fallbackMs / 1000
  return value.endsWith('ms') ? amount / 1000 : amount
}

function pixelToken(name: string, fallback: number): number {
  const value = Number.parseFloat(rootToken(name, `${fallback}px`))
  return Number.isFinite(value) ? value : fallback
}

export function prefersReducedMotion(): boolean {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

function clearMotionStyles(target: HTMLElement): void {
  gsap.set(target, { clearProps: 'transform,opacity,visibility,filter' })
  target.style.removeProperty('transform')
  target.style.removeProperty('opacity')
  target.style.removeProperty('visibility')
  target.style.removeProperty('filter')
}

export function enterRoute(element: Element, done: () => void): void {
  const target = element as HTMLElement
  gsap.killTweensOf(target)

  if (prefersReducedMotion()) {
    clearMotionStyles(target)
    window.requestAnimationFrame(done)
    return
  }

  gsap.fromTo(
    target,
    { autoAlpha: 0, y: pixelToken('--page-slide-distance', 8), filter: `blur(${rootToken('--page-blur', '3px')})` },
    {
      autoAlpha: 1,
      y: 0,
      filter: 'blur(0px)',
      duration: durationToken('--page-slide-dur', 250),
      ease: 'power3.out',
      onComplete: () => {
        clearMotionStyles(target)
        done()
      },
    },
  )
}

export function leaveRoute(element: Element, done: () => void): void {
  const target = element as HTMLElement
  gsap.killTweensOf(target)

  if (prefersReducedMotion()) {
    window.requestAnimationFrame(done)
    return
  }

  gsap.to(target, {
    autoAlpha: 0,
    y: pixelToken('--distance-micro', 4) * -1,
    filter: `blur(${rootToken('--blur-small', '2px')})`,
    duration: durationToken('--duration-quick', 150),
    ease: 'power2.in',
    onComplete: done,
  })
}

export function cancelRouteMotion(element: Element): void {
  const target = element as HTMLElement
  gsap.killTweensOf(target)
  clearMotionStyles(target)
}

export function enterToast(element: Element, done: () => void): void {
  const target = element as HTMLElement
  gsap.killTweensOf(target)

  if (prefersReducedMotion()) {
    clearMotionStyles(target)
    window.requestAnimationFrame(done)
    return
  }

  gsap.fromTo(
    target,
    {
      autoAlpha: 0,
      y: pixelToken('--toast-distance', 16),
      scale: Number.parseFloat(rootToken('--toast-scale', '0.97')),
      filter: `blur(${rootToken('--toast-blur', '2px')})`,
    },
    {
      autoAlpha: 1,
      y: 0,
      scale: 1,
      filter: 'blur(0px)',
      duration: durationToken('--toast-open', 350),
      ease: 'power3.out',
      onComplete: () => {
        clearMotionStyles(target)
        done()
      },
    },
  )
}

export function leaveToast(element: Element, done: () => void): void {
  const target = element as HTMLElement
  gsap.killTweensOf(target)

  if (prefersReducedMotion()) {
    window.requestAnimationFrame(done)
    return
  }

  gsap.to(target, {
    autoAlpha: 0,
    y: pixelToken('--distance-base', 8) * -1,
    scale: Number.parseFloat(rootToken('--toast-scale', '0.97')),
    filter: `blur(${rootToken('--toast-blur', '2px')})`,
    duration: durationToken('--toast-close', 250),
    ease: 'power2.in',
    onComplete: done,
  })
}

function reveal(element: HTMLElement, options: MotionOptions = {}): void {
  const preset = presets[options.preset ?? 'row']
  activeTweens.get(element)?.kill()

  if (prefersReducedMotion()) {
    clearMotionStyles(element)
    return
  }

  const delay = options.delay ?? Math.min(Math.max(options.index ?? 0, 0), 8) * 0.045
  const tween = gsap.fromTo(
    element,
    { autoAlpha: 0, y: preset.y, scale: preset.scale },
    {
      autoAlpha: 1,
      y: 0,
      scale: 1,
      delay,
      duration: preset.duration,
      ease: (options.preset ?? 'row') === 'card' ? 'back.out(1.16)' : 'power3.out',
      onComplete: () => {
        activeTweens.delete(element)
        clearMotionStyles(element)
      },
    },
  )
  activeTweens.set(element, tween)
}

function readOptions(binding: DirectiveBinding<MotionOptions | undefined>): MotionOptions {
  return binding.value ?? {}
}

export const motionDirective: Directive<HTMLElement, MotionOptions | undefined> = {
  mounted(element, binding) {
    reveal(element, readOptions(binding))
  },
  beforeUnmount(element) {
    activeTweens.get(element)?.kill()
    activeTweens.delete(element)
    clearMotionStyles(element)
  },
}
