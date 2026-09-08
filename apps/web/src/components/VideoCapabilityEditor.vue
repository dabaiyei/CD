<script setup lang="ts">
import {
  AudioLines,
  Clapperboard,
  FileText,
  Film,
  GalleryHorizontal,
  Image,
  Minus,
  Plus,
  Rows3,
  Trash2,
  Video,
} from 'lucide-vue-next'

import type {
  DurationResolutionGroup,
  VideoGenerationMode,
  VideoModelCapabilities,
} from '@/types'

const props = defineProps<{ modelValue: VideoModelCapabilities }>()
const emit = defineEmits<{ 'update:modelValue': [value: VideoModelCapabilities] }>()

const modeOptions = [
  { value: 'text_to_video' as const, label: '文生视频', description: '仅使用提示词生成', icon: FileText },
  { value: 'first_frame' as const, label: '首帧生成', description: '首帧图片驱动画面', icon: Image },
  { value: 'first_last_frame' as const, label: '首尾帧', description: '约束起止画面', icon: GalleryHorizontal },
  { value: 'last_frame' as const, label: '尾帧生成', description: '以结尾画面反推', icon: Rows3 },
  { value: 'full_reference' as const, label: '全参考', description: '参考视频动作与镜头', icon: Film },
  { value: 'multi_shot' as const, label: '多镜头', description: '单任务生成连续镜头', icon: Clapperboard },
]

const mediaOptions = [
  { value: 'image' as const, label: '图片参考', icon: Image },
  { value: 'video' as const, label: '视频参考', icon: Video },
  { value: 'audio' as const, label: '音频参考', icon: AudioLines },
]

const audioOptions = [
  { value: 'optional' as const, label: '音频可选', description: '任务可选择是否输出音频' },
  { value: 'required' as const, label: '必须有声', description: '始终请求带音频的视频' },
  { value: 'disabled' as const, label: '仅无声视频', description: '不请求或接收音频轨' },
]

function clone(): VideoModelCapabilities {
  return JSON.parse(JSON.stringify(props.modelValue)) as VideoModelCapabilities
}

function toggleMode(mode: VideoGenerationMode): void {
  const next = clone()
  const index = next.generation_modes.indexOf(mode)
  if (index >= 0 && next.generation_modes.length > 1) next.generation_modes.splice(index, 1)
  else if (index < 0) next.generation_modes.push(mode)
  if (['first_frame', 'first_last_frame', 'last_frame'].includes(mode) && index < 0) {
    next.reference_limits.image = { ...next.reference_limits.image, enabled: true, min_count: 1, max_count: Math.max(1, next.reference_limits.image.max_count) }
  }
  if (mode === 'full_reference' && index < 0) {
    next.reference_limits.video = { ...next.reference_limits.video, enabled: true, min_count: 1, max_count: Math.max(1, next.reference_limits.video.max_count) }
  }
  emit('update:modelValue', next)
}

function toggleReference(kind: 'image' | 'video' | 'audio'): void {
  const next = clone()
  const enabled = !next.reference_limits[kind].enabled
  next.reference_limits[kind] = enabled
    ? { ...next.reference_limits[kind], enabled: true, min_count: 0, max_count: 1 }
    : { ...next.reference_limits[kind], enabled: false, min_count: 0, max_count: 0 }
  emit('update:modelValue', next)
}

function setReferenceFormats(kind: 'image' | 'video' | 'audio', raw: string): void {
  const next = clone()
  next.reference_limits[kind].accepted_mime_types = [
    ...new Set(raw.split(/[,，\s]+/).map((item) => item.trim().toLowerCase()).filter(Boolean)),
  ]
  emit('update:modelValue', next)
}

function stepReference(kind: 'image' | 'video' | 'audio', field: 'min_count' | 'max_count', delta: number): void {
  const next = clone()
  const limit = next.reference_limits[kind]
  if (!limit.enabled) return
  limit[field] = Math.max(0, Math.min(32, limit[field] + delta))
  if (field === 'min_count' && limit.min_count > limit.max_count) limit.max_count = limit.min_count
  if (field === 'max_count' && limit.max_count < limit.min_count) limit.min_count = limit.max_count
  if (limit.max_count === 0) limit.max_count = 1
  emit('update:modelValue', next)
}

function setAudioPolicy(value: VideoModelCapabilities['audio_policy']): void {
  const next = clone()
  next.audio_policy = value
  emit('update:modelValue', next)
}

function setList(field: 'aspect_ratios' | 'prompt_languages', raw: string): void {
  const values = raw.split(/[,，\s]+/).map((item) => item.trim()).filter(Boolean)
  if (!values.length) return
  const next = clone()
  next[field] = [...new Set(values)]
  emit('update:modelValue', next)
}

function setGroupList(index: number, field: keyof DurationResolutionGroup, raw: string): void {
  const next = clone()
  const group = next.duration_resolution_map[index]
  if (!group) return
  if (field === 'durations') {
    const values = raw.split(/[,，\s]+/).map(Number).filter((item) => Number.isFinite(item) && item > 0)
    if (values.length) group.durations = [...new Set(values)]
  } else {
    const values = raw.split(/[,，]+/).map((item) => item.trim()).filter(Boolean)
    if (values.length) group.resolutions = [...new Set(values)]
  }
  emit('update:modelValue', next)
}

function addGroup(): void {
  const next = clone()
  next.duration_resolution_map.push({ durations: [5], resolutions: ['720p'] })
  emit('update:modelValue', next)
}

function removeGroup(index: number): void {
  if (props.modelValue.duration_resolution_map.length === 1) return
  const next = clone()
  next.duration_resolution_map.splice(index, 1)
  emit('update:modelValue', next)
}
</script>

<template>
  <div class="video-capability-editor field--full">
    <section class="capability-section">
      <header><div><strong>视频生成模式</strong><p>选择该模型真实支持的输入与生成方式</p></div><span>{{ modelValue.generation_modes.length }} 项</span></header>
      <div class="mode-option-grid">
        <button v-for="option in modeOptions" :key="option.value" type="button" :class="{ active: modelValue.generation_modes.includes(option.value) }" :aria-pressed="modelValue.generation_modes.includes(option.value)" @click="toggleMode(option.value)">
          <span><component :is="option.icon" :size="18" /></span>
          <div><strong>{{ option.label }}</strong><small>{{ option.description }}</small></div>
          <i></i>
        </button>
      </div>
    </section>

    <section class="capability-section">
      <header><div><strong>参考媒体限制</strong><p>定义单个任务允许上传的媒体类型和数量</p></div></header>
      <div class="reference-limit-grid">
        <article v-for="media in mediaOptions" :key="media.value" :class="{ enabled: modelValue.reference_limits[media.value].enabled }">
          <button type="button" class="reference-limit__toggle" :aria-pressed="modelValue.reference_limits[media.value].enabled" @click="toggleReference(media.value)">
            <span><component :is="media.icon" :size="18" /></span><strong>{{ media.label }}</strong><i></i>
          </button>
          <div class="reference-steppers" :aria-disabled="!modelValue.reference_limits[media.value].enabled">
            <label><span>最少</span><div><button type="button" title="减少最少数量" @click="stepReference(media.value, 'min_count', -1)"><Minus :size="14" /></button><strong class="tabular-nums">{{ modelValue.reference_limits[media.value].min_count }}</strong><button type="button" title="增加最少数量" @click="stepReference(media.value, 'min_count', 1)"><Plus :size="14" /></button></div></label>
            <label><span>最多</span><div><button type="button" title="减少最多数量" @click="stepReference(media.value, 'max_count', -1)"><Minus :size="14" /></button><strong class="tabular-nums">{{ modelValue.reference_limits[media.value].max_count }}</strong><button type="button" title="增加最多数量" @click="stepReference(media.value, 'max_count', 1)"><Plus :size="14" /></button></div></label>
          </div>
          <label class="reference-formats">
            <span>支持格式（MIME）</span>
            <input :value="modelValue.reference_limits[media.value].accepted_mime_types.join(', ')" :disabled="!modelValue.reference_limits[media.value].enabled" placeholder="image/jpeg, image/png" @change="setReferenceFormats(media.value, ($event.target as HTMLInputElement).value)" />
          </label>
        </article>
      </div>
    </section>

    <section class="capability-section">
      <header><div><strong>音频输出策略</strong><p>决定视频任务是否携带音频轨</p></div></header>
      <div class="audio-policy" role="radiogroup" aria-label="音频输出策略">
        <button v-for="option in audioOptions" :key="option.value" type="button" role="radio" :aria-checked="modelValue.audio_policy === option.value" :class="{ active: modelValue.audio_policy === option.value }" @click="setAudioPolicy(option.value)"><span><AudioLines :size="17" /></span><div><strong>{{ option.label }}</strong><small>{{ option.description }}</small></div></button>
      </div>
    </section>

    <section class="capability-section duration-map-section">
      <header><div><strong>时长与分辨率映射</strong><p>同一组内的时长可使用该组全部分辨率</p></div><button class="button button--secondary" type="button" @click="addGroup"><Plus :size="15" />添加组合</button></header>
      <div class="duration-map-list">
        <article v-for="(group, index) in modelValue.duration_resolution_map" :key="index">
          <span class="duration-map-index tabular-nums">{{ String(index + 1).padStart(2, '0') }}</span>
          <label><span>时长（秒，逗号分隔）</span><input :value="group.durations.join(', ')" inputmode="decimal" @change="setGroupList(index, 'durations', ($event.target as HTMLInputElement).value)" /></label>
          <i>→</i>
          <label><span>分辨率（逗号分隔）</span><input :value="group.resolutions.join(', ')" @change="setGroupList(index, 'resolutions', ($event.target as HTMLInputElement).value)" /></label>
          <button type="button" class="icon-button icon-button--small icon-button--danger" title="删除组合" :disabled="modelValue.duration_resolution_map.length === 1" @click="removeGroup(index)"><Trash2 :size="15" /></button>
        </article>
      </div>
    </section>

    <section class="capability-section capability-meta-grid">
      <label class="field"><span>影片比例</span><input :value="modelValue.aspect_ratios.join(', ')" placeholder="16:9, 9:16, 1:1" @change="setList('aspect_ratios', ($event.target as HTMLInputElement).value)" /><small>支持逗号或空格分隔</small></label>
      <label class="field"><span>提示词语言</span><input :value="modelValue.prompt_languages.join(', ')" placeholder="zh-CN, en-US" @change="setList('prompt_languages', ($event.target as HTMLInputElement).value)" /><small>使用标准语言代码</small></label>
      <button type="button" class="capability-switch" :aria-pressed="modelValue.negative_prompt_supported" @click="$emit('update:modelValue', { ...clone(), negative_prompt_supported: !modelValue.negative_prompt_supported })"><span><i></i></span><div><strong>负面提示词</strong><small>模型支持独立负面提示词字段</small></div></button>
      <button type="button" class="capability-switch" :aria-pressed="modelValue.asynchronous" @click="$emit('update:modelValue', { ...clone(), asynchronous: !modelValue.asynchronous })"><span><i></i></span><div><strong>异步任务</strong><small>创建后通过任务 ID 轮询结果</small></div></button>
    </section>
  </div>
</template>
