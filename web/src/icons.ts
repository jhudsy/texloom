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
  gear: 'M18.67 9.88 L21.03 10.24 L21.03 13.76 L18.67 14.12 L18.21 15.22 L19.63 17.14 L17.14 19.63 L15.22 18.21 L14.12 18.67 L13.76 21.03 L10.24 21.03 L9.88 18.67 L8.78 18.21 L6.86 19.63 L4.37 17.14 L5.79 15.22 L5.33 14.12 L2.97 13.76 L2.97 10.24 L5.33 9.88 L5.79 8.78 L4.37 6.86 L6.86 4.37 L8.78 5.79 L9.88 5.33 L10.24 2.97 L13.76 2.97 L14.12 5.33 L15.22 5.79 L17.14 4.37 L19.63 6.86 L18.21 8.78Z M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0z',
  folder: 'M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z',
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
