<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { AlignLeft, Check, CircleAlert, Clapperboard, Film, Image as ImageIcon, ImagePlus, LoaderCircle, MonitorUp, Palette, Plus, Ratio, Search, Sparkles, Trash2, Type } from 'lucide-vue-next'
import gsap from 'gsap'

import AgentChatPanel from '@/components/AgentChatPanel.vue'
import BaseDialog from '@/components/BaseDialog.vue'
import ProjectCard from '@/components/ProjectCard.vue'
import UiSelect from '@/components/UiSelect.vue'
import { ApiError } from '@/lib/api'
import { useAuthStore } from '@/stores/auth'
import { useProjectsStore, type ProjectPayload } from '@/stores/projects'
import { useToastStore } from '@/stores/toast'
import type { Project } from '@/types'

const router = useRouter()
const auth = useAuthStore()
const projectStore = useProjectsStore()
const toast = useToastStore()

const workspaceRoot = ref<HTMLElement | null>(null)
const query = ref('')
const dialogOpen = ref(false)
const editingId = ref<string | null>(null)
const saving = ref(false)
const generatingCover = ref(false)
const uploadingCover = ref(false)
const coverInput = ref<HTMLInputElement | null>(null)
const pendingCover = ref<File | null>(null)
const pendingCoverPreview = ref<string | null>(null)
const deleteTarget = ref<Project | null>(null)
const deleting = ref(false)

const emptyForm = (): Partial<ProjectPayload> & Pick<ProjectPayload, 'name'> => ({
  name: '',
  description: '',
  cover_url: null,
  video_model_id: null,
  video_resolution: '720p',
  aspect_ratio: '16:9',
  image_resolution: '1K',
  visual_handbook_id: null,
  director_handbook_id: null,
})
const form = reactive(emptyForm())
const coverPreview = computed(() => pendingCoverPreview.value || form.cover_url || '/covers/login-studio.jpg')
const coverPrice = computed(() => Number(
  projectStore.pricing.find((rule) => rule.task_type === 'project_cover_generation')?.unit_cost ?? 20,
).toFixed(2))
const videoModelOptions = computed(() => (projectStore.options?.video_models ?? []).map((item) => ({ value: item.id, label: item.name, description: '视频生成模型', icon: Film })))
const videoResolutionOptions = computed(() => (projectStore.options?.video_resolutions ?? []).map((item) => ({ value: item, label: item, description: '视频输出清晰度', icon: MonitorUp })))
const aspectRatioOptions = computed(() => (projectStore.options?.aspect_ratios ?? []).map((item) => ({ value: item, label: item, description: '成片画幅比例', icon: Ratio })))
const imageResolutionOptions = computed(() => (projectStore.options?.image_resolutions ?? []).map((item) => ({ value: item, label: item, description: '图片输出清晰度', icon: ImageIcon })))
const projectConfigurationReady = computed(() => Boolean(
  form.video_model_id
  && form.visual_handbook_id
  && form.director_handbook_id,
))
const missingConfigurationLabels = computed(() => [
  !form.video_model_id ? '视频模型' : '',
  !form.visual_handbook_id ? '视觉手册' : '',
  !form.director_handbook_id ? '导演手册' : '',
].filter(Boolean))

const filteredProjects = computed(() => {
  const keyword = query.value.trim().toLowerCase()
  if (!keyword) return projectStore.projects
  return projectStore.projects.filter(
    (project) => project.name.toLowerCase().includes(keyword) || project.description.toLowerCase().includes(keyword),
  )
})

let workspaceMotion: gsap.MatchMedia | undefined

function setupWorkspaceMotion(): void {
  const root = workspaceRoot.value
  if (!root) return

  workspaceMotion = gsap.matchMedia()
  workspaceMotion.add('(prefers-reduced-motion: no-preference)', () => {
    const context = gsap.context(() => {
      const targets = gsap.utils.toArray<HTMLElement>('.workspace-reveal')
      gsap.fromTo(
        targets,
        { autoAlpha: 0, y: window.matchMedia('(max-width: 860px)').matches ? 8 : 14 },
        {
          autoAlpha: 1,
          y: 0,
          duration: 0.46,
          stagger: 0.075,
          ease: 'power3.out',
          onComplete: () => {
            targets.forEach((target) => {
              target.style.removeProperty('transform')
              target.style.removeProperty('opacity')
              target.style.removeProperty('visibility')
            })
          },
        },
      )
    }, root)
    return () => context.revert()
  })
  workspaceMotion.add('(prefers-reduced-motion: reduce)', () => {
    root.querySelectorAll<HTMLElement>('.workspace-reveal').forEach((target) => {
      target.style.removeProperty('transform')
      target.style.removeProperty('opacity')
      target.style.removeProperty('visibility')
    })
  })
}

onMounted(async () => {
  setupWorkspaceMotion()
  try {
    await projectStore.load()
  } catch (error) {
    toast.show('项目加载失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  }
})

onBeforeUnmount(() => {
  workspaceMotion?.revert()
  clearPendingCover()
})

function clearPendingCover(): void {
  if (pendingCoverPreview.value) URL.revokeObjectURL(pendingCoverPreview.value)
  pendingCover.value = null
  pendingCoverPreview.value = null
  if (coverInput.value) coverInput.value.value = ''
}

function resetForm(): void {
  clearPendingCover()
  Object.assign(form, emptyForm())
  const options = projectStore.options
  form.video_model_id = options?.video_models[0]?.id ?? null
  form.visual_handbook_id = options?.visual_handbooks[0]?.id ?? null
  form.director_handbook_id = options?.director_handbooks[0]?.id ?? null
}

function setImageResolution(value: string): void {
  if (value === '1K' || value === '2K' || value === '4K') form.image_resolution = value
}

function openCreate(): void {
  editingId.value = null
  resetForm()
  dialogOpen.value = true
}

function openSettings(project: Project): void {
  clearPendingCover()
  editingId.value = project.id
  Object.assign(form, project)
  dialogOpen.value = true
}

function requestDelete(project: Project): void {
  deleteTarget.value = project
}

async function deleteProject(): Promise<void> {
  const target = deleteTarget.value
  if (!target || deleting.value) return
  deleting.value = true
  try {
    await projectStore.remove(target.id)
    deleteTarget.value = null
    toast.show('项目已删除', { message: `“${target.name}”及其项目数据已清理`, tone: 'success' })
  } catch (error) {
    toast.show('无法删除项目', {
      message: error instanceof Error ? error.message : undefined,
      tone: 'error',
    })
  } finally {
    deleting.value = false
  }
}

async function saveProject(): Promise<void> {
  saving.value = true
  try {
    if (editingId.value) {
      await projectStore.update(editingId.value, form)
      toast.show('项目设置已保存', { tone: 'success' })
    } else {
      const created = await projectStore.create(form)
      if (pendingCover.value) {
        uploadingCover.value = true
        try {
          await projectStore.uploadCover(created.id, pendingCover.value)
        } catch (error) {
          toast.show('项目已创建，但封面上传失败', {
            message: error instanceof Error ? error.message : undefined,
            tone: 'error',
          })
        } finally {
          uploadingCover.value = false
        }
      }
      toast.show('项目已创建', { tone: 'success' })
      dialogOpen.value = false
      clearPendingCover()
      await router.push(`/projects/${created.id}/director`)
      return
    }
    dialogOpen.value = false
  } catch (error) {
    toast.show('保存失败', { message: error instanceof ApiError ? error.message : undefined, tone: 'error' })
  } finally {
    saving.value = false
  }
}

async function selectCover(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) {
    toast.show('图片格式不支持', { message: '请选择 JPG、PNG 或 WebP 图片', tone: 'error' })
    input.value = ''
    return
  }
  if (file.size > 8 * 1024 * 1024) {
    toast.show('图片过大', { message: '项目封面不能超过 8 MB', tone: 'error' })
    input.value = ''
    return
  }

  clearPendingCover()
  pendingCover.value = file
  pendingCoverPreview.value = URL.createObjectURL(file)

  if (!editingId.value) return
  uploadingCover.value = true
  try {
    const project = await projectStore.uploadCover(editingId.value, file)
    form.cover_url = project.cover_url
    clearPendingCover()
    toast.show('项目封面已更新', { tone: 'success' })
  } catch (error) {
    toast.show('封面上传失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    uploadingCover.value = false
  }
}

async function generateCover(): Promise<void> {
  if (!editingId.value) return
  generatingCover.value = true
  try {
    await projectStore.generateCover(editingId.value)
    await auth.refreshSession()
    toast.show('封面任务已进入队列', { message: `本次已扣除 ${coverPrice.value} 积分`, tone: 'success' })
  } catch (error) {
    toast.show('无法生成封面', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    generatingCover.value = false
  }
}
</script>

<template>
  <div ref="workspaceRoot" class="workspace-page page-stack">
    <section class="workspace-agent-stage workspace-reveal workspace-reveal--agent" aria-label="AI 创作助手">
      <AgentChatPanel personal :pricing="projectStore.pricing" />
    </section>

    <div class="workspace-command-grid workspace-command-grid--projects">
      <main class="project-library workspace-reveal workspace-reveal--toolbar">
        <header class="project-library__header">
          <div><span>PROJECT LIBRARY</span><h2>制作项目</h2></div>
          <section class="workspace-toolbar" aria-label="项目筛选">
            <label class="search-field">
              <Search :size="17" aria-hidden="true" />
              <input v-model="query" type="search" placeholder="搜索项目" aria-label="搜索项目" />
            </label>
            <span class="result-count tabular-nums">{{ filteredProjects.length }} 个项目</span>
            <button class="button button--primary workspace-create-button" type="button" @click="openCreate">
              <Plus :size="17" />
              <span>新建项目</span>
            </button>
          </section>
        </header>

        <section v-if="projectStore.loading" class="project-grid" aria-label="正在加载项目">
          <div v-for="index in 6" :key="index" class="project-skeleton">
            <span></span><span></span><span></span>
          </div>
        </section>

        <section v-else-if="filteredProjects.length" class="project-grid" aria-label="短剧项目">
          <ProjectCard
            v-for="(project, index) in filteredProjects"
            :key="project.id"
            :project="project"
            v-motion="{ preset: 'card', index }"
            @open="router.push(`/projects/${project.id}/director`)"
            @settings="openSettings(project)"
            @delete="requestDelete(project)"
          />
          <button v-motion="{ preset: 'card', index: filteredProjects.length }" class="new-project-tile" type="button" @click="openCreate">
            <span><Plus :size="21" /></span>
            <strong>新建短剧项目</strong>
          </button>
        </section>

        <section v-else class="empty-state">
          <span class="empty-state__icon"><Search :size="22" /></span>
          <h2>没有匹配的项目</h2>
          <p>调整关键词后重新搜索</p>
        </section>
      </main>

    </div>

    <BaseDialog
      v-model:open="dialogOpen"
      :title="editingId ? '项目设置' : '创建短剧项目'"
      :description="editingId ? '调整当前项目的生成模型与创作手册' : '设置项目基础信息与生成偏好'"
      wide
    >
      <form id="project-form" class="project-form" @submit.prevent="saveProject">
        <div class="project-form__main">
          <label class="field field--full">
            <span class="field-caption">项目名称</span>
            <div class="project-text-control">
              <span><Type :size="17" /></span>
              <input v-model.trim="form.name" maxlength="120" required placeholder="输入短剧名称" />
            </div>
          </label>
          <label class="field field--full">
            <span class="field-caption">项目简介</span>
            <div class="project-text-control project-text-control--textarea">
              <span><AlignLeft :size="17" /></span>
              <textarea v-model.trim="form.description" rows="4" maxlength="4000" placeholder="简要描述故事与创作方向"></textarea>
            </div>
          </label>
          <div class="field">
            <span>视频模型</span>
            <UiSelect :model-value="form.video_model_id ?? ''" :options="videoModelOptions" placeholder="选择视频模型" @update:model-value="form.video_model_id = $event" />
          </div>
          <div class="field">
            <span>视频分辨率</span>
            <UiSelect :model-value="form.video_resolution ?? ''" :options="videoResolutionOptions" placeholder="选择视频分辨率" @update:model-value="form.video_resolution = $event" />
          </div>
          <div class="field">
            <span>影片比例</span>
            <UiSelect :model-value="form.aspect_ratio ?? ''" :options="aspectRatioOptions" placeholder="选择影片比例" @update:model-value="form.aspect_ratio = $event" />
          </div>
          <div class="field">
            <span>图片分辨率</span>
            <UiSelect :model-value="form.image_resolution ?? ''" :options="imageResolutionOptions" placeholder="选择图片分辨率" @update:model-value="setImageResolution" />
          </div>
          <section class="project-handbook-picker field--full">
            <header><span>视觉手册</span><small>{{ projectStore.options?.visual_handbooks.find((item) => item.id === form.visual_handbook_id)?.name || '未选择' }}</small></header>
            <div v-if="projectStore.options?.visual_handbooks.length" class="handbook-choice-grid" role="radiogroup" aria-label="视觉手册">
              <button v-for="(handbook, index) in projectStore.options.visual_handbooks" :key="handbook.id" class="handbook-choice" :class="{ selected: form.visual_handbook_id === handbook.id }" :style="{ '--choice-index': index }" type="button" :aria-pressed="form.visual_handbook_id === handbook.id" @click="form.visual_handbook_id = handbook.id">
                <span class="handbook-choice__cover"><img :src="handbook.cover_url || '/covers/login-studio.jpg'" :alt="handbook.name" /><Palette :size="15" /></span>
                <span class="handbook-choice__copy"><strong>{{ handbook.name }}</strong><span>{{ handbook.description }}</span><small class="tabular-nums">v{{ handbook.version }}</small></span>
                <span class="handbook-choice__check"><Check :size="15" /></span>
              </button>
            </div>
            <div v-else class="handbook-choice-empty"><Palette :size="20" /><span>暂无可用视觉手册</span></div>
          </section>
          <section class="project-handbook-picker field--full">
            <header><span>导演手册</span><small>{{ projectStore.options?.director_handbooks.find((item) => item.id === form.director_handbook_id)?.name || '未选择' }}</small></header>
            <div v-if="projectStore.options?.director_handbooks.length" class="handbook-choice-grid" role="radiogroup" aria-label="导演手册">
              <button v-for="(handbook, index) in projectStore.options.director_handbooks" :key="handbook.id" class="handbook-choice" :class="{ selected: form.director_handbook_id === handbook.id }" :style="{ '--choice-index': index }" type="button" :aria-pressed="form.director_handbook_id === handbook.id" @click="form.director_handbook_id = handbook.id">
                <span class="handbook-choice__cover"><img :src="handbook.cover_url || '/covers/second-farewell.jpg'" :alt="handbook.name" /><Clapperboard :size="15" /></span>
                <span class="handbook-choice__copy"><strong>{{ handbook.name }}</strong><span>{{ handbook.description }}</span><small class="tabular-nums">v{{ handbook.version }}</small></span>
                <span class="handbook-choice__check"><Check :size="15" /></span>
              </button>
            </div>
            <div v-else class="handbook-choice-empty"><Clapperboard :size="20" /><span>暂无可用导演手册</span></div>
          </section>
          <div class="project-config-status field--full" :data-ready="projectConfigurationReady">
            <Check v-if="projectConfigurationReady" :size="17" />
            <CircleAlert v-else :size="17" />
            <span><strong>{{ projectConfigurationReady ? '生产配置完整' : '生产配置待完善' }}</strong><small>{{ projectConfigurationReady ? '模型与创作手册已就绪' : `还需配置：${missingConfigurationLabels.join('、')}` }}</small></span>
          </div>
        </div>
        <aside class="cover-panel">
          <span class="field-label">项目封面</span>
          <div class="cover-preview">
            <img :src="coverPreview" alt="项目封面预览" />
          </div>
          <input
            ref="coverInput"
            class="hidden-file-input"
            type="file"
            accept="image/jpeg,image/png,image/webp"
            @change="selectCover"
          />
          <div class="cover-panel__actions">
            <button
              class="button button--secondary"
              type="button"
              :disabled="uploadingCover"
              @click="coverInput?.click()"
            >
              <LoaderCircle v-if="uploadingCover" class="spin" :size="17" />
              <ImagePlus v-else :size="17" />
              {{ uploadingCover ? '上传中' : '上传' }}
            </button>
            <button
              class="button button--secondary"
              type="button"
              :disabled="!editingId || generatingCover"
              @click="generateCover"
            >
              <LoaderCircle v-if="generatingCover" class="spin" :size="17" />
              <Sparkles v-else :size="17" />
              AI 生成
            </button>
          </div>
          <p>AI 生成消耗 <strong class="tabular-nums">{{ coverPrice }}</strong> 积分</p>
        </aside>
      </form>
      <template #footer>
        <button class="button button--ghost" type="button" @click="dialogOpen = false">取消</button>
        <button class="button button--primary" form="project-form" type="submit" :disabled="saving || !projectConfigurationReady" :title="projectConfigurationReady ? '' : `还需配置：${missingConfigurationLabels.join('、')}`">
          <LoaderCircle v-if="saving" class="spin" :size="17" />
          {{ editingId ? '保存设置' : '创建项目' }}
        </button>
      </template>
    </BaseDialog>

    <BaseDialog
      :open="Boolean(deleteTarget)"
      title="确认删除项目"
      description="此操作不可撤销"
      @update:open="!$event && !deleting && (deleteTarget = null)"
    >
      <div class="danger-confirm">
        <span><Trash2 :size="22" /></span>
        <div>
          <strong>删除“{{ deleteTarget?.name }}”</strong>
          <p>章节、剧本、项目资产、文件、分镜、媒体、对话与任务历史都会永久删除。已经导出到全局资产库的副本不受影响。</p>
        </div>
      </div>
      <template #footer>
        <button class="button button--ghost" type="button" :disabled="deleting" @click="deleteTarget = null">取消</button>
        <button class="button button--danger" type="button" :disabled="deleting" @click="deleteProject">
          <LoaderCircle v-if="deleting" class="spin" :size="17" />
          <Trash2 v-else :size="17" />
          {{ deleting ? '删除中' : '确认删除' }}
        </button>
      </template>
    </BaseDialog>
  </div>
</template>
