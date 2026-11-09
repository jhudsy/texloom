// A detailed word count from TeXCount, the standard LaTeX word counter. It is a Perl program, run
// in a Worker on the Perl the in-browser TeX engine already ships for Biber, so nothing leaves
// this device. The script is public/vendor/texcount.pl; the instant counts are in navigate.ts.
import { hasAsset } from './compile'
import { dialog, h } from './dom'
import { locale, t } from './i18n'
import { cleanPath, isTexFile, mainFile } from './projectfiles'
import type { Replica } from './replica'

const ENGINE = '/core/busytex'
const TIME_LIMIT = 120_000

const FIELDS: [english: string, label: () => string][] = [
  ['Words in text', () => t('Words in text')],
  ['Words in headers', () => t('Words in headers')],
  ['Words outside text (captions, etc.)', () => t('Words outside text (captions, etc.)')],
  ['Number of headers', () => t('Headers')],
  ['Number of floats/tables/figures', () => t('Floats, tables and figures')],
  ['Number of math inlines', () => t('Inline maths')],
  ['Number of math displayed', () => t('Displayed maths')],
]
interface Block { name: string; values: Map<string, number> }

/** TeXCount counts one script at a time, so the project's text chooses which. */
export function languageOption(text: string): string[] {
  if (/[\p{sc=Hiragana}\p{sc=Katakana}]/u.test(text)) return ['-japanese']
  if (/\p{sc=Han}/u.test(text)) return ['-chinese']
  if (/\p{sc=Hangul}/u.test(text)) return ['-korean-words']
  return []
}

/** Its report: one block per file and, with several files, one for their total. */
export function parseReport(output: string): { files: Block[]; total: Block | null; notes: string[] } {
  const blocks = output.split(/\n\s*\n/).map((chunk): Block => {
    const values = new Map<string, number>()
    let name = ''
    for (const line of chunk.split('\n')) {
      const m = /^([^:]+): (.*)$/.exec(line.trim())
      if (!m) continue
      if (/^\d+$/.test(m[2])) values.set(m[1], Number(m[2]))
      else if (/^(?:Included file|File|File\(s\) total)$/.test(m[1])) name = m[1] === 'File(s) total' ? '' : m[2].replace(/^\.\//, '')
    }
    return { name, values }
  }).filter((b) => b.values.has('Words in text'))
  const sum = output.includes('File(s) total:') ? blocks.pop() ?? null : null
  return { files: blocks, total: sum ?? blocks[0] ?? null, notes: output.split('\n').filter((l) => l.startsWith('!!!')) }
}

function run(r: Replica, main: string): Promise<string> {
  return fetch('/vendor/texcount.pl').then((res) => {
    if (!res.ok) throw new Error(String(res.status))
    return res.text()
  }).then((script) => new Promise<string>((resolve, reject) => {
    const files: [string, string][] = []
    r.files.forEach((text, p) => { const path = cleanPath(p); if (path) files.push([path, text.toString()]) })
    const worker = new Worker('/texcount-worker.js')
    const finish = (act: () => void) => { clearTimeout(timer); worker.terminate(); act() }
    const timer = setTimeout(() => finish(() => reject(new Error(t('TeXCount took too long and was stopped.')))), TIME_LIMIT)
    worker.onmessage = (e: MessageEvent<{ status: number; output: string }>) => finish(() => (e.data.status ? reject(new Error(e.data.output.trim().slice(-500))) : resolve(e.data.output)))
    worker.onerror = (e) => finish(() => reject(new Error(e.message)))
    const texts = files.filter(([p]) => isTexFile(p)).map(([, text]) => text).join('\n')
    worker.postMessage({ base: ENGINE, script, files, main, args: ['-inc', '-utf8', '-nc', ...languageOption(texts)] })
  }))
}

export function texCountDialog(r: Replica) {
  const body = h('div', { role: 'status' }, h('p', { class: 'muted' }, t('Counting… The first time, this downloads about 30 MB.')))
  const dlg = dialog('settings',
    h('h3', {}, t('Detailed word count')),
    body,
    h('p', { class: 'muted small' }, t('Counted by TeXCount on this device. Nothing is sent to the server.')),
    h('div', { class: 'row end' }, h('button', { class: 'primary', onclick: () => dlg.close() }, t('Close'))))

  const number = (b: Block, key: string) => (b.values.get(key) ?? 0).toLocaleString(locale)
  const failed = (message: string) => body.replaceChildren(h('p', { class: 'err' }, message))
  const main = mainFile(r)
  if (!main) { failed(t('No .tex file with \\documentclass found.')); return }
  hasAsset(`${ENGINE}/biber.wasm`).then(async (available) => {
    if (!available) { failed(t('TeXCount isn’t available on this server.')); return }
    const { files, total, notes } = parseReport(await run(r, main))
    if (!total) { failed(t('TeXCount couldn’t count this project.')); return }
    body.replaceChildren(...[
      h('table', { class: 'texcount' }, ...FIELDS.map(([key, label]) => h('tr', {}, h('td', {}, label()), h('td', { class: 'num' }, number(total, key))))),
      files.length > 1 ? h('table', { class: 'texcount files' },
        h('tr', {}, h('th', {}, t('File')), h('th', { class: 'num' }, t('Text')), h('th', { class: 'num' }, t('Headers')), h('th', { class: 'num' }, t('Other'))),
        ...files.map((f) => h('tr', {}, h('td', {}, f.name), ...FIELDS.slice(0, 3).map(([key]) => h('td', { class: 'num' }, number(f, key)))))) : null,
      notes.length ? h('pre', { class: 'muted small' }, notes.join('\n')) : null,
    ].filter((x) => x !== null))
  }).catch((e) => failed(t('TeXCount couldn’t count this project.') + ` ${(e as Error).message ?? e}`))
}
