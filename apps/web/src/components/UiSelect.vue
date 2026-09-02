<script setup lang="ts">
import type { Component } from 'vue'
import { computed } from 'vue'
import { Check, ChevronDown } from 'lucide-vue-next'
import {
  SelectContent,
  SelectItem,
  SelectItemIndicator,
  SelectItemText,
  SelectPortal,
  SelectRoot,
  SelectTrigger,
  SelectViewport,
} from 'reka-ui'

export interface SelectOption {
  value: string
  label: string
  description?: string
  icon?: Component
}

const props = withDefaults(
  defineProps<{
    modelValue: string
    options: SelectOption[]
    placeholder?: string
    disabled?: boolean
    variant?: 'default' | 'compact' | 'brand' | 'history'
  }>(),
  { placeholder: '请选择', disabled: false, variant: 'default' },
)
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()
const selected = computed(() => props.options.find((item) => item.value === props.modelValue))
</script>

<template>
  <SelectRoot
    :model-value="modelValue"
    :disabled="disabled"
    @update:model-value="(value) => value != null && emit('update:modelValue', String(value))"
  >
    <SelectTrigger class="ui-select" :class="`ui-select--${variant}`" :aria-label="placeholder">
      <span class="ui-select__value">
        <component :is="selected.icon" v-if="selected?.icon" :size="16" />
        <span>{{ selected?.label || placeholder }}</span>
      </span>
      <ChevronDown class="ui-select__chevron" :size="16" />
    </SelectTrigger>
    <SelectPortal>
      <SelectContent class="ui-select-menu" position="popper" :side-offset="6">
        <SelectViewport class="ui-select-menu__viewport">
          <SelectItem v-for="option in options" :key="option.value" class="ui-select-option" :value="option.value">
            <span class="ui-select-option__icon">
              <component :is="option.icon" v-if="option.icon" :size="16" />
            </span>
            <SelectItemText>
              <span class="ui-select-option__copy">
                <strong>{{ option.label }}</strong>
                <small v-if="option.description">{{ option.description }}</small>
              </span>
            </SelectItemText>
            <SelectItemIndicator class="ui-select-option__check"><Check :size="15" /></SelectItemIndicator>
          </SelectItem>
        </SelectViewport>
      </SelectContent>
    </SelectPortal>
  </SelectRoot>
</template>
