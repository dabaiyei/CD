<script setup lang="ts">
import { ref } from 'vue'
import { ChevronRight, FileText, Folder } from 'lucide-vue-next'

import type { SkillNode } from '@/types'

const props = withDefaults(defineProps<{ nodes: SkillNode[]; selected?: string; depth?: number }>(), {
  depth: 0,
})
const emit = defineEmits<{ select: [node: SkillNode] }>()
const collapsed = ref(new Set(
  props.depth > 0
    ? props.nodes.filter((node) => node.kind === 'directory').map((node) => node.path)
    : [],
))

function toggle(node: SkillNode): void {
  const next = new Set(collapsed.value)
  if (next.has(node.path)) next.delete(node.path)
  else next.add(node.path)
  collapsed.value = next
}
</script>

<template>
  <ul class="skill-tree">
    <li v-for="node in nodes" :key="node.path">
      <button
        class="skill-node"
        :class="{
          'skill-node--active': selected === node.path,
          'skill-node--directory': node.kind === 'directory',
          'skill-node--collapsed': node.kind === 'directory' && collapsed.has(node.path),
        }"
        type="button"
        @click="node.kind === 'file' ? emit('select', node) : toggle(node)"
      >
        <ChevronRight v-if="node.kind === 'directory'" :size="14" />
        <Folder v-if="node.kind === 'directory'" :size="16" />
        <FileText v-else :size="16" />
        <span>{{ node.name }}</span>
      </button>
      <SkillTree
        v-if="node.children.length && !collapsed.has(node.path)"
        :nodes="node.children"
        :selected="selected"
        :depth="depth + 1"
        @select="emit('select', $event)"
      />
    </li>
  </ul>
</template>
