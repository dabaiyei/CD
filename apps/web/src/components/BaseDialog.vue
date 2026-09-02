<script setup lang="ts">
import { X } from 'lucide-vue-next'
import {
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogOverlay,
  DialogPortal,
  DialogRoot,
  DialogTitle,
} from 'reka-ui'

defineProps<{
  open: boolean
  title: string
  description?: string
  wide?: boolean
  workbench?: boolean
}>()

const emit = defineEmits<{ 'update:open': [value: boolean] }>()
</script>

<template>
  <DialogRoot :open="open" @update:open="emit('update:open', $event)">
    <DialogPortal>
      <DialogOverlay class="dialog-overlay" />
      <DialogContent
        class="dialog-content"
        :class="{ 'dialog-content--wide': wide, 'dialog-content--workbench': workbench }"
      >
        <header class="dialog-header">
          <div>
            <DialogTitle class="dialog-title">{{ title }}</DialogTitle>
            <DialogDescription :class="description ? 'dialog-description' : 'sr-only'">
              {{ description || `${title}操作窗口` }}
            </DialogDescription>
          </div>
          <DialogClose class="icon-button" title="关闭">
            <X :size="18" />
          </DialogClose>
        </header>
        <div class="dialog-body"><slot /></div>
        <footer v-if="$slots.footer" class="dialog-footer"><slot name="footer" /></footer>
      </DialogContent>
    </DialogPortal>
  </DialogRoot>
</template>
