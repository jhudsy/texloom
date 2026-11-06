// Parse a TeX/LaTeX log into problems (errors, warnings, bad boxes) with project
// file and line, for a clickable Problems list and editor markers.

export interface Problem {
  severity: 'error' | 'warning' | 'box'
  file: string | null // project path, or null if not in the project / unknown
  line: number | null
  message: string
}

/** Map a path from the log to a project path (or null if it's a system file). */
function toProject(path: string | null, files: string[]): string | null {
  if (!path) return null
  const p = path.replace(/\/\.\//g, '/').replace(/^\.\//, '')
  if (files.includes(p)) return p
  return files.find((f) => p.endsWith('/' + f)) ?? files.find((f) => p.endsWith('/' + f.replace(/\.tex$/, '')) || p === f.replace(/\.tex$/, '')) ?? null
}

export function parseLog(log: string, files: string[]): Problem[] {
  const lines = log.split('\n')
  const out: Problem[] = []
  const seen = new Set<string>()
  const add = (p: Problem) => {
    const key = `${p.severity}|${p.file}|${p.line}|${p.message}`
    if (!seen.has(key)) { seen.add(key); out.push(p) }
  }
  const stack: (string | null)[] = [] // open "(file" groups; null for ordinary parentheses
  const current = () => { for (let i = stack.length - 1; i >= 0; i--) if (stack[i]) return stack[i]; return null }

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    const fileAtStart = current()

    // -file-line-error style: ./main.tex:12: message
    const fle = /^(\.?\/?[^:\s][^:]*\.(?:tex|sty|cls|bib|ltx|bbl)):(\d+): (.*)$/.exec(line)
    if (/==> Fatal error occurred|^! Emergency stop/.test(line)) { /* summary noise, not a problem */ }
    else if (fle) add({ severity: 'error', file: toProject(fle[1], files), line: +fle[2], message: fle[3].trim() })
    else if (line.startsWith('! ')) {
      let lineNo: number | null = null
      for (let j = i + 1; j < Math.min(lines.length, i + 12); j++) {
        const m = /^l\.(\d+)/.exec(lines[j])
        if (m) { lineNo = +m[1]; break }
      }
      add({ severity: 'error', file: toProject(fileAtStart, files), line: lineNo, message: line.slice(2).trim() })
    }

    const warn = /^(?:LaTeX|Package (\S+)|Class (\S+)) Warning: (.*)$/.exec(line)
    if (warn) {
      let msg = warn[3]
      for (let j = i + 1; j < lines.length && lines[j].trim() && j < i + 6; j++) msg += ' ' + lines[j].replace(/^\(\S+\)\s*/, '').trim()
      const at = /on input line (\d+)/.exec(msg)
      add({ severity: 'warning', file: at ? toProject(fileAtStart, files) : null, line: at ? +at[1] : null,
        message: `${warn[1] ?? warn[2] ? `${warn[1] ?? warn[2]}: ` : ''}${msg.replace(/\s+/g, ' ').trim()}` })
    }

    const box = /^((?:Over|Under)full \\[hv]box \([^)]*\)).*?(?:at lines? (\d+)|detected at line (\d+))/.exec(line)
    if (box) add({ severity: 'box', file: toProject(fileAtStart, files), line: +(box[2] ?? box[3]), message: box[1] })

    // track which file the following lines belong to: "(path" opens, ")" closes
    for (let c = 0; c < line.length; c++) {
      const ch = line[c]
      if (ch === '(') {
        const m = /^\(([^\s()[\]{}"]+\.[A-Za-z0-9]+)/.exec(line.slice(c))
        stack.push(m && !/^\d/.test(m[1]) ? m[1] : null)
      } else if (ch === ')') stack.pop()
    }
  }
  const rank = { error: 0, warning: 1, box: 2 }
  return out.sort((a, b) => rank[a.severity] - rank[b.severity])
}
