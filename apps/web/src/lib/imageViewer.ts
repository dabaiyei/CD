import { shallowRef, type Directive } from 'vue'

export const imageViewer = shallowRef<{ src: string; title: string; trigger: HTMLElement } | null>(null)
const cleanup = new WeakMap<HTMLElement, () => void>()

export const imagePreviewDirective: Directive<HTMLImageElement, string | null | undefined> = {
  mounted(el, binding) {
    let source = binding.value
    const open = (event: Event) => {
      if (event instanceof KeyboardEvent && !['Enter', ' '].includes(event.key)) return
      if (!el.complete || !el.naturalWidth) return
      event.preventDefault()
      event.stopPropagation()
      imageViewer.value = { src: source || el.currentSrc || el.src, title: el.alt || '图片预览', trigger: el }
    }
    el.dataset.previewSource = source || ''
    const click = (event: Event) => { source = el.dataset.previewSource; open(event) }
    el.tabIndex = 0
    el.setAttribute('role', 'button')
    el.setAttribute('aria-label', `放大预览：${el.alt || '图片'}`)
    el.title = '点击放大预览'
    el.classList.add('image-preview-trigger')
    el.addEventListener('click', click)
    el.addEventListener('keydown', click)
    cleanup.set(el, () => {
      el.removeEventListener('click', click)
      el.removeEventListener('keydown', click)
    })
  },
  updated(el, binding) {
    el.dataset.previewSource = binding.value || ''
    el.setAttribute('aria-label', `放大预览：${el.alt || '图片'}`)
  },
  beforeUnmount(el) { cleanup.get(el)?.(); cleanup.delete(el) },
}
