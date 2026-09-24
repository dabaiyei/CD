/** Pure geometry shared by virtual lists; no DOM reads during scrolling. */
export function gridGeometry(width: number, minimum: number, gap: number, ratio = 0, footer = 0) {
  const columns = Math.max(1, Math.floor((Math.max(0, width) + gap) / (minimum + gap)))
  const cardWidth = Math.max(0, (width - gap * (columns - 1)) / columns)
  return { columns, cardWidth, cardHeight: ratio > 0 ? cardWidth / ratio + footer : 0 }
}

export function scrollOrigin(top: number, parentTop: number, offset: number, border = 0) {
  return top - parentTop + offset - border
}

export function chapterExtent(horizontal: boolean, width: number) {
  return horizontal ? Math.min(312, Math.max(180, width - 24)) : 96
}
