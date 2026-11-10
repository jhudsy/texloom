// Small line icons for buttons, drawn in the text colour. Inline SVG, so they look the same on
// every system (symbol characters such as ⎙ or ⧉ depend on the fonts installed).
const PATHS = {
  plus: 'M12 5v14M5 12h14',
  chevron: 'M7 10l5 5 5-5',
  upload: 'M12 20V8M7 13l5-5 5 5M5 4h14',
  download: 'M12 4v12M7 11l5 5 5-5M5 20h14',
  print: 'M7 9V4h10v5M7 17H5a2 2 0 0 1-2-2v-4a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2h-2M7 14h10v6H7z',
  window: 'M14 4h6v6M20 4l-8 8M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4',
  pencil: 'M4 20h4L19 9l-4-4L4 16v4zM13 7l4 4',
  copy: 'M10 9h9a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1h-9a1 1 0 0 1-1-1v-9a1 1 0 0 1 1-1zM5 15H4.5A1.5 1.5 0 0 1 3 13.5v-9A1.5 1.5 0 0 1 4.5 3h9A1.5 1.5 0 0 1 15 4.5V5',
  close: 'M6 6l12 12M18 6L6 18',
} as const
export type IconName = keyof typeof PATHS

const NS = 'http://www.w3.org/2000/svg'

export function icon(name: IconName): SVGSVGElement {
  const svg = document.createElementNS(NS, 'svg')
  svg.setAttribute('viewBox', '0 0 24 24')
  svg.setAttribute('class', `ico ico-${name}`)
  svg.setAttribute('aria-hidden', 'true')
  const path = document.createElementNS(NS, 'path')
  path.setAttribute('d', PATHS[name])
  svg.append(path)
  return svg
}
