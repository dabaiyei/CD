/** Per-file limit. The server verifies actual image bytes and normalizes to WebP. */
export const MAX_IMAGE_UPLOAD_BYTES = 100 * 1024 * 1024

/**
 * Formats the server accepts. Phones and Windows apps save JPEGs under several
 * extensions (`jpe`, `jfif`, and `mpo` for HDR/portrait/burst shots), and some
 * browsers report an empty MIME type for them, so the name is checked too.
 * This is only a fast pre-check; the API validates the decoded pixels.
 */
const SUPPORTED_MIME_TYPES = ['image/jpeg', 'image/jpg', 'image/pjpeg', 'image/png', 'image/webp']
const SUPPORTED_EXTENSIONS = /\.(jpe?g|jpe|jfif|mpo|png|webp)$/i

export function isSupportedImage(file: File): boolean {
  const mime = file.type.toLowerCase().split(';')[0]?.trim()
  return SUPPORTED_MIME_TYPES.includes(mime || '') || SUPPORTED_EXTENSIONS.test(file.name)
}

/**
 * `accept` for image file pickers. Extensions are listed because Windows and
 * Android photo pickers hide files whose MIME type the browser cannot resolve.
 */
export const IMAGE_ACCEPT_ATTRIBUTE =
  '.jpg,.jpeg,.jpe,.jfif,.mpo,.png,.webp,image/jpeg,image/png,image/webp'

/** Best-effort local compression. Unsupported browsers keep the server path. */
export async function prepareChatImage(file: File, signal: AbortSignal): Promise<File> {
  if (signal.aborted) throw new DOMException('上传已取消', 'AbortError')
  if (file.size > MAX_IMAGE_UPLOAD_BYTES) throw new Error('单张图片不能超过 100MB')
  if (typeof Worker === 'undefined' || typeof OffscreenCanvas === 'undefined') return file
  return new Promise<File>((resolve, reject) => {
    let worker: Worker
    try { worker = new Worker(new URL('./chatImage.worker.ts', import.meta.url), { type: 'module' }) }
    catch { resolve(file); return }
    const finish = (blob?: Blob, cancelled = false) => {
      clearTimeout(timer)
      signal.removeEventListener('abort', abort)
      worker.terminate()
      if (cancelled) { reject(new DOMException('上传已取消', 'AbortError')); return }
      // Never increase network traffic just to change the file extension.
      resolve(blob && blob.size < file.size
        ? new File([blob], `${file.name.replace(/\.[^.]+$/, '') || 'image'}.webp`, { type: 'image/webp' })
        : file)
    }
    const abort = () => finish(undefined, true)
    const timer = setTimeout(() => finish(), 30_000)
    signal.addEventListener('abort', abort, { once: true })
    worker.onmessage = (event: MessageEvent<{ blob?: Blob }>) => finish(event.data.blob)
    worker.onerror = () => finish()
    worker.onmessageerror = () => finish()
    try { worker.postMessage(file) } catch { finish() }
  })
}
