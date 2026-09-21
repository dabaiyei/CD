import type { Chapter } from '@/types'

/**
 * The chapter's character count, before or after its body is fetched.
 *
 * The chapter list reports `content_length` instead of the text, so a reader can
 * show the size immediately; once the body loads, the exact length takes over.
 */
export function chapterContentLength(chapter: Chapter | null | undefined): number {
  if (!chapter) return 0
  if (typeof chapter.original_content === 'string') return chapter.original_content.length
  return chapter.content_length ?? 0
}
