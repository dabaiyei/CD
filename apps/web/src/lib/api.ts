import type { ApiErrorPayload } from '@/types'

const TOKEN_KEY = 'cineforge.access-token'
const AUTH_ENDPOINTS = new Set(['/auth/login', '/auth/refresh', '/auth/logout'])

let refreshRequest: Promise<string> | null = null

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message)
  }
}

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string | null): void {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

async function refreshAccessToken(): Promise<string> {
  if (!refreshRequest) {
    refreshRequest = fetch('/api/v1/auth/refresh', {
      method: 'POST',
      credentials: 'same-origin',
    })
      .then(async (response) => {
        if (!response.ok) throw new ApiError('登录状态已过期，请重新登录', response.status)
        const body = (await response.json()) as { access_token: string }
        setToken(body.access_token)
        return body.access_token
      })
      .catch((error: unknown) => {
        setToken(null)
        window.dispatchEvent(new Event('cineforge:auth-expired'))
        throw error
      })
      .finally(() => {
        refreshRequest = null
      })
  }
  return refreshRequest
}

async function request<T>(path: string, init: RequestInit, allowRefresh: boolean): Promise<T> {
  const headers = new Headers(init.headers)
  const token = getToken()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  if (init.body && !(init.body instanceof FormData)) headers.set('Content-Type', 'application/json')

  const response = await fetch(`/api/v1${path}`, { ...init, credentials: 'same-origin', headers })
  if (response.status === 401 && allowRefresh && !AUTH_ENDPOINTS.has(path)) {
    await refreshAccessToken()
    return request<T>(path, init, false)
  }
  if (!response.ok) {
    let message = `请求失败 (${response.status})`
    try {
      const body = (await response.json()) as ApiErrorPayload
      if (typeof body.detail === 'string') message = body.detail
      else if (Array.isArray(body.detail)) message = body.detail.map((item) => item.msg).join('；')
    } catch {
      // Keep the status-based fallback when the upstream response is not JSON.
    }
    throw new ApiError(message, response.status)
  }
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  return request<T>(path, init, true)
}

export interface ApiStreamMessage<T> {
  event: string
  data: T
}

async function streamRequest<T>(
  path: string,
  onMessage: (message: ApiStreamMessage<T>) => void,
  signal: AbortSignal,
  allowRefresh: boolean,
): Promise<void> {
  const headers = new Headers({ Accept: 'text/event-stream' })
  const token = getToken()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const response = await fetch(`/api/v1${path}`, { credentials: 'same-origin', headers, signal })
  if (response.status === 401 && allowRefresh) {
    await refreshAccessToken()
    return streamRequest(path, onMessage, signal, false)
  }
  if (!response.ok || !response.body) throw new ApiError(`实时连接失败 (${response.status})`, response.status)

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (!signal.aborted) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let boundary = buffer.match(/\r?\n\r?\n/)
    while (boundary?.index !== undefined) {
      const block = buffer.slice(0, boundary.index)
      buffer = buffer.slice(boundary.index + boundary[0].length)
      let event = 'message'
      const data: string[] = []
      for (const line of block.split(/\r?\n/)) {
        if (line.startsWith('event:')) event = line.slice(6).trim()
        if (line.startsWith('data:')) data.push(line.slice(5).trimStart())
      }
      if (data.length) {
        try {
          onMessage({ event, data: JSON.parse(data.join('\n')) as T })
        } catch {
          // Ignore malformed or forward-incompatible event payloads; database polling reconciles state.
        }
      }
      boundary = buffer.match(/\r?\n\r?\n/)
    }
  }
}

export async function apiEventStream<T>(
  path: string,
  onMessage: (message: ApiStreamMessage<T>) => void,
  signal: AbortSignal,
): Promise<void> {
  return streamRequest(path, onMessage, signal, true)
}
