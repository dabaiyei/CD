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
  section: { y: 12, scale: 1, duration: 0.42 },
  card: { y: 14, scale: 0.985, duration: 0.4 },
  row: { y: 8, scale: 0.992, duration: 0.32 },
}

export function prefersReducedMotion(): boolean {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

function clearMotionStyles(target: HTMLElement): void {
  gsap.set(target, { clearProps: 'transform,opacity,visibility' })
  target.style.removeProperty('transform')
  target.style.removeProperty('opacity')
  target.style.removeProperty('visibility')
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
    { autoAlpha: 0, y: 10 },
    {
      autoAlpha: 1,
      y: 0,
      duration: 0.34,
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
    y: -4,
    duration: 0.14,
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
    { autoAlpha: 0, y: 12, scale: 0.97 },
    {
      autoAlpha: 1,
      y: 0,
      scale: 1,
      duration: 0.3,
      ease: 'back.out(1.35)',
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
    y: -8,
    scale: 0.985,
    duration: 0.15,
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
      ease: 'power3.out',
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
