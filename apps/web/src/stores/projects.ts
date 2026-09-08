import { ref } from 'vue'
import { defineStore } from 'pinia'

import { api } from '@/lib/api'
import type { PricingRule, Project, ProjectOptions } from '@/types'

export type ProjectPayload = Omit<Project, 'id' | 'tenant_id' | 'owner_id' | 'created_at' | 'updated_at' | 'creation_state'>

export const useProjectsStore = defineStore('projects', () => {
  const projects = ref<Project[]>([])
  const options = ref<ProjectOptions | null>(null)
  const pricing = ref<PricingRule[]>([])
  const loading = ref(false)

  async function load(): Promise<void> {
    loading.value = true
    try {
      const [projectRows, optionRows, pricingRows] = await Promise.all([
        api<Project[]>('/projects'),
        api<ProjectOptions>('/projects/options'),
        api<PricingRule[]>('/pricing'),
      ])
      projects.value = projectRows
      options.value = optionRows
      pricing.value = pricingRows
    } finally {
      loading.value = false
    }
  }

  async function get(id: string): Promise<Project> {
    const existing = projects.value.find((item) => item.id === id)
    if (existing) return existing
    return api<Project>(`/projects/${id}`)
  }

  async function create(payload: Partial<ProjectPayload> & Pick<ProjectPayload, 'name'>): Promise<Project> {
    const project = await api<Project>('/projects', { method: 'POST', body: JSON.stringify(payload) })
    projects.value.unshift(project)
    return project
  }

  async function update(id: string, payload: Partial<ProjectPayload>): Promise<Project> {
    const project = await api<Project>(`/projects/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    })
    const index = projects.value.findIndex((item) => item.id === id)
    if (index >= 0) projects.value[index] = project
    return project
  }

  async function remove(id: string): Promise<void> {
    await api<void>(`/projects/${id}`, { method: 'DELETE' })
    const index = projects.value.findIndex((item) => item.id === id)
    if (index >= 0) projects.value.splice(index, 1)
  }

  async function generateCover(id: string, styleHint?: string): Promise<void> {
    await api(`/projects/${id}/cover/generate`, {
      method: 'POST',
      body: JSON.stringify({ style_hint: styleHint || null }),
    })
  }

  async function uploadCover(id: string, file: File): Promise<Project> {
    const body = new FormData()
    body.append('file', file)
    const project = await api<Project>(`/projects/${id}/cover/upload`, { method: 'POST', body })
    const index = projects.value.findIndex((item) => item.id === id)
    if (index >= 0) projects.value[index] = project
    return project
  }

  return { projects, options, pricing, loading, load, get, create, update, remove, generateCover, uploadCover }
})
