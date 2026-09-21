import type { StoryboardShot } from '@/types'

/**
 * Whether a shot already has a video prompt.
 *
 * The board listing sends only `has_video_prompt`; the text itself arrives when
 * the shot is opened. Checking both keeps every caller correct in either state,
 * so a status badge is right before and after the body is fetched.
 */
export function shotHasVideoPrompt(shot: StoryboardShot | null | undefined): boolean {
  if (!shot) return false
  if (typeof shot.video_prompt === 'string') return shot.video_prompt.trim().length > 0
  return Boolean(shot.has_video_prompt)
}

/** Whether a shot already has a first-frame image prompt. */
export function shotHasImagePrompt(shot: StoryboardShot | null | undefined): boolean {
  if (!shot) return false
  if (typeof shot.image_prompt === 'string') return shot.image_prompt.trim().length > 0
  return Boolean(shot.has_image_prompt)
}
