function legacyCopyText(value: string): boolean {
  const textarea = document.createElement('textarea')
  textarea.value = value
  textarea.readOnly = true
  textarea.tabIndex = -1
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
  const selection = window.getSelection()
  const ranges = selection ? Array.from({ length: selection.rangeCount }, (_, i) => selection.getRangeAt(i).cloneRange()) : []
  const inputSelection = activeElement instanceof HTMLInputElement || activeElement instanceof HTMLTextAreaElement
    ? { start: activeElement.selectionStart, end: activeElement.selectionEnd } : null
  // Stay within the active dialog's focus trap (including native modal dialogs).
  const container = activeElement?.closest('dialog, [role="dialog"], [role="alertdialog"]') ?? document.body

  let copied = false
  try {
    container.appendChild(textarea)
    textarea.focus({ preventScroll: true })
    textarea.select()
    textarea.setSelectionRange(0, value.length)
    copied = document.execCommand('copy')
  } catch {
    copied = false
  } finally {
    textarea.remove()
    activeElement?.focus({ preventScroll: true })
    if (inputSelection?.start != null && inputSelection.end != null
      && (activeElement instanceof HTMLInputElement || activeElement instanceof HTMLTextAreaElement)) {
      activeElement.setSelectionRange(inputSelection.start, inputSelection.end)
    } else if (selection && ranges.length) {
      selection.removeAllRanges()
      ranges.forEach(range => selection.addRange(range))
    }
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
