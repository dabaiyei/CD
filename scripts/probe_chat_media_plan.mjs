import assert from 'node:assert/strict'
import puppeteer from '../services/video-replica/node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js'

const browser = await puppeteer.launch({ executablePath: `${process.env.LOCALAPPDATA}/ms-playwright/chromium-1228/chrome-win64/chrome.exe`, headless: true })
try {
  for (const width of [1366, 390]) {
    const page = await browser.newPage()
    page.on('pageerror', error => console.log('Page error:', error.message))
    await page.setViewport({ width, height: 900 })
    await page.evaluateOnNewDocument(() => localStorage.setItem('cineforge.access-token', 'ui-test'))
    let phase = 'image'
    let videoPolls = 0
    const plan = () => ({ source_message_id: 'draft', status: phase === 'done' ? 'completed' : 'ready',
      index: phase === 'image' ? 0 : phase === 'done' ? 2 : 1, auto_continue: true,
      completed: phase === 'image' ? [] : [{ index: 0 }],
      steps: [{ type: 'image', options: { aspect_ratio: '9:16', resolution: '1K' } },
        { type: 'video', use_previous_image: true, options: { aspect_ratio: '9:16', resolution: '720P', duration_seconds: 12 } }] })
    const task = (id) => ({ id, task_type: 'agent_chat_run',
      status: id === 'first' ? phase === 'image' ? 'running' : 'succeeded' : phase === 'done' ? 'succeeded' : phase === 'failed' ? 'failed' : 'running',
      progress: 48, latest_message: id === 'second' ? '正在检查上一步图片是否满足已确认方案' : '正在生成图片',
      error_message: phase === 'failed' && id === 'second' ? '视频平台暂时不可用，图片已保存' : null,
      request_payload: { agent_chat_session_id: 's', original_mode: 'chat', mode: id === 'first' ? 'image' : 'video', jev_route: { media_plan: { ...plan(), index: id === 'first' ? 0 : 1 } } },
      created_at: new Date().toISOString() })
    const session = () => ({ id: 's', title: '连续创作测试', runtime_manifest: { mode: 'chat', media_plan: plan() }, created_at: new Date().toISOString(), last_message_at: new Date().toISOString() })
    const messages = () => [
      { id: 'u1', role: 'user', content: '开始，图片完成后自动生成视频', run_id: 'first' },
      ...(phase === 'image' ? [] : [{ id: 'a1', role: 'assistant', content: '图片已保存，正在自动续接下一步。', run_id: 'first' },
        { id: 'u2', role: 'user', content: '【系统自动续接】生成视频', run_id: 'second' }]),
      ...(['done', 'failed'].includes(phase) ? [{ id: 'a2', role: 'assistant', content: phase === 'done' ? '视频生成完成' : '视频生成失败，图片已保留', run_id: 'second' }] : []),
    ].map(item => ({ ...item, runtime_manifest: {}, runtime_events: [], created_at: new Date().toISOString() }))
    await page.setRequestInterception(true)
    page.on('request', req => {
      const p = new URL(req.url()).pathname
      if (!p.startsWith('/api/')) return req.continue()
      let data = []
      if (p.endsWith('/auth/me')) data = { user: { id: 'u', role: 'admin', display_name: '测试', background_blur: 0 }, credit_balance: '100' }
      else if (p.endsWith('/tasks') || p.endsWith('/notifications')) data = { items: [], next_before: null, unread_count: 0 }
      else if (p.endsWith('/projects/options')) data = { text_models: [], image_models: [], video_models: [], tts_models: [], visual_handbooks: [], director_handbooks: [], styles: [], genres: [] }
      else if (p.endsWith('/agent/options')) data = { agents: [{ id: 'a', name: 'AI' }], skills: [], text_models: [], image_models: [], video_models: [], tts_models: [] }
      else if (p.endsWith('/agent/sessions')) data = [session()]
      else if (p.endsWith('/agent/sessions/s')) data = { session: session(), messages: messages(), active_task: ['done', 'failed'].includes(phase) ? null : task(phase === 'image' ? 'first' : 'second') }
      else if (p.endsWith('/tasks/first')) data = task('first')
      else if (p.endsWith('/tasks/second')) { videoPolls++; data = task('second') }
      req.respond({ status: 200, contentType: 'application/json', body: JSON.stringify(data) })
    })
    await page.goto('http://127.0.0.1:5173/workspace', { waitUntil: 'networkidle0' })
    await page.click('[title="打开历史记录"]')
    await page.waitForSelector('.agent-history-entry__open')
    await page.click('.agent-history-entry__open')
    assert.equal(await page.$('.agent-media-plan'), null, 'progress must not appear inline or open automatically')
    const placement = await page.evaluate(() => {
      const button = document.querySelector('.agent-task-trigger').getBoundingClientRect()
      const mode = document.querySelector('.agent-mode-dropdown > summary').getBoundingClientRect()
      return { left: button.left, right: button.right, modeLeft: mode.left, modeRight: mode.right, height: button.height }
    })
    assert(placement.left >= 0 && placement.right <= placement.modeLeft && placement.modeRight <= width && placement.height >= 40, 'task button must fit left of mode selector')
    await page.click('.agent-task-trigger')
    await page.waitForSelector('.agent-media-plan')
    phase = 'video'
    await page.waitForFunction(() => document.querySelector('.agent-media-plan')?.textContent.includes('第 2/2 步'))
    await page.waitForFunction(() => document.querySelector('.agent-media-plan')?.textContent.includes('检查上一步图片'))
    if (!videoPolls) await page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/tasks/second'))
    assert(videoPolls > 0, 'UI must follow the automatically queued video task')
    const box = await page.$eval('.agent-media-plan', el => ({ left: el.getBoundingClientRect().left, right: el.getBoundingClientRect().right }))
    assert(box.left >= 0 && box.right <= width, 'plan must fit viewport')
    await page.screenshot({ path: `.logs/media-plan-${width}.png`, fullPage: true })
    phase = width === 390 ? 'failed' : 'done'
    await page.waitForFunction(text => document.querySelector('.agent-media-plan')?.textContent.includes(text), {}, phase === 'done' ? '连续创作已完成' : '失败待重试')
    await page.screenshot({ path: `.logs/media-plan-final-${width}.png`, fullPage: true })
    await page.keyboard.press('Escape')
    await page.waitForSelector('.agent-media-plan', { hidden: true })
    await page.click('.agent-task-trigger')
    await page.waitForFunction(text => document.querySelector('.agent-media-plan')?.textContent.includes(text), {}, phase === 'done' ? '连续创作已完成' : '失败待重试')
    await page.click('.agent-task-dialog [title="关闭"]')
    await page.waitForSelector('.agent-media-plan', { hidden: true })
    console.log(`${width}px: automatic handoff, current stage and terminal status passed`)
    await page.close()
  }
} finally { await browser.close() }
