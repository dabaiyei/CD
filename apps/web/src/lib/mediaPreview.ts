export function mediaPreview(url: string | null | undefined, edge: 320 | 640 = 320): string {
  if (!url || !url.startsWith('/uploads/')) return url || ''
  const parsed = new URL(url, window.location.origin)
  parsed.searchParams.set('thumbnail', String(edge))
  return `${parsed.pathname}${parsed.search}${parsed.hash}`
}
