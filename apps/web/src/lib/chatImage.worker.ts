/// <reference lib="webworker" />

// A worker per image bounds memory across repeated uploads and keeps H5 responsive.
self.onmessage = async (event: MessageEvent<File>) => {
  let bitmap: ImageBitmap | undefined
  try {
    bitmap = await createImageBitmap(event.data, { imageOrientation: 'from-image' })
    if (bitmap.width * bitmap.height > 100_000_000) throw new Error('图片像素过大')
    const scale = Math.min(1, 4096 / Math.max(bitmap.width, bitmap.height))
    const canvas = new OffscreenCanvas(Math.max(1, Math.round(bitmap.width * scale)),
      Math.max(1, Math.round(bitmap.height * scale)))
    const context = canvas.getContext('2d')
    if (!context) throw new Error('浏览器无法处理图片')
    context.drawImage(bitmap, 0, 0, canvas.width, canvas.height)
    bitmap.close()
    bitmap = undefined
    const blob = await canvas.convertToBlob({ type: 'image/webp', quality: 0.9 })
    if (blob.type !== 'image/webp' || !blob.size) throw new Error('浏览器不支持 WebP 编码')
    self.postMessage({ blob })
  } catch {
    self.postMessage({ error: true })
  } finally {
    bitmap?.close()
  }
}
