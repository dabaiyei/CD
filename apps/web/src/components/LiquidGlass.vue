<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { gsap } from 'gsap'

const props = withDefaults(defineProps<{
  as?: string
  interactive?: boolean
  intensity?: 'subtle' | 'medium' | 'strong'
}>(), {
  as: 'div',
  interactive: true,
  intensity: 'medium',
})

const root = ref<HTMLElement | null>(null)
let media: gsap.MatchMedia | undefined
let removePointer: (() => void) | undefined

onMounted(() => {
  if (!root.value || !props.interactive) return

    const element = root.value
    media = gsap.matchMedia()
    media.add(
      {
        desktop: '(hover: hover) and (pointer: fine) and (min-width: 720px)',
        reduced: '(prefers-reduced-motion: reduce)',
      },
      ({ conditions }) => {
        if (!conditions?.desktop || conditions.reduced || !root.value) return

        const setX = gsap.quickTo(element, '--liquid-pointer-x', {
          duration: 0.42,
          ease: 'power3.out',
          overwrite: 'auto',
        })
        const setY = gsap.quickTo(element, '--liquid-pointer-y', {
          duration: 0.42,
          ease: 'power3.out',
          overwrite: 'auto',
        })

        const onPointerMove = (event: PointerEvent) => {
          const bounds = element.getBoundingClientRect()
          if (!bounds.width || !bounds.height) return
          setX(((event.clientX - bounds.left) / bounds.width) * 100)
          setY(((event.clientY - bounds.top) / bounds.height) * 100)
        }
        const onPointerLeave = () => {
          setX(50)
          setY(20)
        }

        element.addEventListener('pointermove', onPointerMove, { passive: true })
        element.addEventListener('pointerleave', onPointerLeave, { passive: true })
        removePointer = () => {
          element.removeEventListener('pointermove', onPointerMove)
          element.removeEventListener('pointerleave', onPointerLeave)
        }
        onPointerLeave()

        return () => {
          removePointer?.()
          removePointer = undefined
        }
      },
      element,
    )
})

onBeforeUnmount(() => {
  removePointer?.()
  media?.revert()
})
</script>

<template>
  <component
    :is="as"
    ref="root"
    class="liquid-glass"
    :class="[
      `liquid-glass--${intensity}`,
      { 'liquid-glass--interactive': interactive },
    ]"
  >
    <span class="liquid-glass__sheen" aria-hidden="true"></span>
    <slot />
  </component>
</template>

<style scoped>
.liquid-glass {
  --liquid-pointer-x: 50;
  --liquid-pointer-y: 20;
  --liquid-glass-fill: color-mix(in srgb, var(--surface-strong, #fff) 68%, transparent);
  --liquid-glass-line: color-mix(in srgb, var(--line, #cbd5e1) 72%, transparent);
  position: relative;
  isolation: isolate;
  overflow: visible;
  border: 1px solid var(--liquid-glass-line);
  background: var(--liquid-glass-fill) !important;
  box-shadow:
    inset 0 1px 0 color-mix(in srgb, #fff 26%, transparent),
    0 12px 32px color-mix(in srgb, #000 10%, transparent);
  -webkit-backdrop-filter: blur(18px) saturate(135%);
  backdrop-filter: blur(18px) saturate(135%);
}

.liquid-glass--subtle {
  -webkit-backdrop-filter: blur(12px) saturate(120%);
  backdrop-filter: blur(12px) saturate(120%);
}

.liquid-glass--strong {
  -webkit-backdrop-filter: blur(26px) saturate(150%);
  backdrop-filter: blur(26px) saturate(150%);
}

.liquid-glass__sheen {
  position: absolute;
  z-index: 0;
  inset: 0;
  border-radius: inherit;
  pointer-events: none;
  background:
    radial-gradient(
      260px circle at calc(var(--liquid-pointer-x) * 1%) calc(var(--liquid-pointer-y) * 1%),
      color-mix(in srgb, #fff 18%, transparent),
      transparent 68%
    ),
    linear-gradient(115deg, color-mix(in srgb, #fff 11%, transparent), transparent 42%);
  opacity: 0.78;
  transition: opacity 220ms ease;
}

.liquid-glass:not(.liquid-glass--interactive) .liquid-glass__sheen {
  opacity: 0.52;
}

@media (hover: hover) and (pointer: fine) {
  .liquid-glass--interactive:hover .liquid-glass__sheen {
    opacity: 1;
  }
}

@media (prefers-reduced-motion: reduce) {
  .liquid-glass__sheen {
    transition: none;
  }
}

@media (max-width: 719px) {
  .liquid-glass {
    -webkit-backdrop-filter: blur(14px) saturate(125%);
    backdrop-filter: blur(14px) saturate(125%);
  }
}
</style>
