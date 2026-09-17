/** Per-file limit. The server verifies actual image bytes and normalizes to WebP. */
export const MAX_IMAGE_UPLOAD_BYTES = 100 * 1024 * 1024

export function isSupportedImage(file: File): boolean {
  const mime = file.type.toLowerCase().split(';')[0]?.trim()
  return ['image/jpeg', 'image/jpg', 'image/png', 'image/webp'].includes(mime || '')
    || /\.(jpe?g|png|webp)$/i.test(file.name)
}
