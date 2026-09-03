function legacyCopyText(value: string): boolean {
  const textarea = document.createElement('textarea')
  textarea.value = value
  textarea.readOnly = true
  textarea.setAttribute('aria-hidden', 'true')
  Object.assign(textarea.style, {
    position: 'fixed',
    inset: '0 auto auto -9999px',
    width: '1px',
    height: '1px',
    padding: '0',
    border: '0',
    opacity: '0',
    fontSize: '16px',
  })

  const activeElement = document.activeElement instanceof HTMLElement ? document.activeElement : null
  document.body.appendChild(textarea)
  textarea.focus({ preventScroll: true })
  textarea.select()
  textarea.setSelectionRange(0, value.length)

  let copied = false
  try {
    copied = document.execCommand('copy')
  } catch {
    copied = false
  } finally {
    textarea.remove()
    activeElement?.focus({ preventScroll: true })
  }
  return copied
}

export async function copyText(value: string): Promise<boolean> {
  if (!window.isSecureContext || !navigator.clipboard?.writeText) {
    return legacyCopyText(value)
  }

  try {
    await navigator.clipboard.writeText(value)
    return true
  } catch {
    return legacyCopyText(value)
  }
}
