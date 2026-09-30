export type ReplicaPlan = {
  summary: string
  shots: { start: number; end: number; observation: string; prompt: string; scene_id?: string; boundary?: 'auto' | 'continuous' | 'cut' }[]
}

export type ReplicaReference = {
  kind: 'attachment' | 'asset' | 'image_task'
  id: string
  purpose: string
  url: string
  name: string
  role?: 'character' | 'scene' | 'style' | 'composition'
  target?: string
  shot_indices?: number[]
}

export function taskPlan(task: { request_payload: Record<string, any>; result_payload?: Record<string, any> | null }): ReplicaPlan | null {
  const value = task.request_payload.editor_draft?.plan || task.result_payload?.plan || task.request_payload.options?.plan
  if (value?.shots) return copyPlan(value)
  const batches = Object.entries(task.result_payload?.batches || {})
    .sort(([a], [b]) => Number(a) - Number(b)).map(([, value]) => value as ReplicaPlan)
  return batches.length ? { summary: batches.map(b => b.summary).join('\n'), shots: batches.flatMap(b => copyPlan(b).shots) } : null
}

// Vue proxies cannot be passed to structuredClone. Copy the editable schema
// explicitly so edits never mutate the saved task or its nested shot objects.
export function copyPlan(source: ReplicaPlan): ReplicaPlan {
  return { summary: source.summary, shots: source.shots.map(shot => ({
    start: shot.start, end: shot.end, observation: shot.observation, prompt: shot.prompt,
    ...(shot.scene_id !== undefined ? { scene_id: shot.scene_id } : {}),
    ...(shot.boundary !== undefined ? { boundary: shot.boundary } : {}),
  })) }
}
