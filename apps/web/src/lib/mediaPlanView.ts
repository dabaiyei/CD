type Data = Record<string, any>
const object = (value: unknown): Data => value && typeof value === 'object' ? value as Data : {}

export function mediaPlanView(raw: unknown, rawTask: unknown) {
  const plan = object(raw)
  if (!Array.isArray(plan.steps) || !plan.steps.length) return null
  const task = object(rawTask)
  const route = object(object(task.request_payload).jev_route)
  const taskPlan = object(route.media_plan)
  const matches = taskPlan.source_message_id === plan.source_message_id && !['text', 'clarify'].includes(route.output)
  const index = matches ? Number(taskPlan.index ?? 0) : Number(plan.index ?? 0)
  const completed = new Set((Array.isArray(plan.completed) ? plan.completed : []).map((item: Data) => Number(item.index)))
  const steps = plan.steps.map((rawStep: unknown, i: number) => {
    const step = object(rawStep)
    let status = completed.has(i) || plan.status === 'completed' ? 'done' : 'waiting'
    if (matches && i === index && status !== 'done') {
      if (task.status === 'succeeded') status = 'done'
      else if (task.status === 'failed') status = 'failed'
      else if (task.status === 'cancelled') status = 'paused'
      else if (task.status === 'queued') status = 'queued'
      else if (task.status === 'running') status = 'running'
    } else if (i === index && status !== 'done') {
      status = plan.status === 'cancelled' || plan.status === 'blocked' ? 'paused' : 'ready'
    }
    const options = object(step.options)
    return { index: i, type: step.type, title: step.type === 'video' ? '生成视频' : '生成图片', status,
      label: ({ done: '已完成', failed: '失败待重试', paused: '已暂停', queued: '排队中', running: '进行中',
        ready: '待开始', waiting: '等待前一步' } as Data)[status] as string,
      specs: [options.aspect_ratio, options.resolution, options.duration_seconds ? `${options.duration_seconds} 秒` : ''].filter(Boolean).join(' · '),
      dependency: step.use_previous_image ? '使用上一步生成图' : '',
    }
  })
  const done = steps.filter(step => step.status === 'done').length
  const current = steps.find(step => ['running', 'queued', 'failed', 'paused', 'ready'].includes(step.status))
  return { steps, done, total: steps.length,
    title: done === steps.length ? '连续创作已完成' : `连续创作 · 第 ${(current?.index ?? index) + 1}/${steps.length} 步`,
    mode: plan.auto_continue ? '自动续接' : '逐步确认',
    detail: matches && task.status !== 'succeeded' ? String(task.error_message || task.latest_message || '') : '',
    failed: steps.some(step => step.status === 'failed'),
  }
}
