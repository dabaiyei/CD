import { ref } from 'vue'
import { defineStore } from 'pinia'

export type ToastTone = 'success' | 'error' | 'info'

interface ToastMessage {
  id: number
  title: string
  message?: string
  tone: ToastTone
}

let nextId = 1

export const useToastStore = defineStore('toast', () => {
  const messages = ref<ToastMessage[]>([])

  function show(title: string, options: { message?: string; tone?: ToastTone } = {}): void {
    const item: ToastMessage = {
      id: nextId++,
      title,
      message: options.message,
      tone: options.tone ?? 'info',
    }
    messages.value.push(item)
    window.setTimeout(() => dismiss(item.id), 3600)
  }

  function dismiss(id: number): void {
    messages.value = messages.value.filter((item) => item.id !== id)
  }

  return { messages, show, dismiss }
})

