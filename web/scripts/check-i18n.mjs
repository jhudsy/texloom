// Checks the French catalogue against the code: every t()/tn()/tNodes() text needs an entry,
// plural texts need [one, other], and placeholders must match. Run: node scripts/check-i18n.mjs
import { parseAst } from 'rolldown/parseAst'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

const root = new URL('../src/', import.meta.url).pathname
const files = (d) => readdirSync(d).flatMap((f) => { const p = join(d, f); return statSync(p).isDirectory() ? files(p) : p.endsWith('.ts') ? [p] : [] })
const parse = (p) => { const src = readFileSync(p, 'utf8'); return { ast: parseAst(src, { lang: 'ts' }, p), line: (i) => src.slice(0, i).split('\n').length } }
const literal = (n) => (!n ? null : n.type === 'Literal' && typeof n.value === 'string' ? n.value
  : n.type === 'TemplateLiteral' && n.expressions.length === 0 ? n.quasis[0].value.cooked : null)
/** Every node in an ESTree, depth first. */
function walk(n, fn) {
  if (!n || typeof n !== 'object') return
  if (Array.isArray(n)) { for (const x of n) walk(x, fn); return }
  if (typeof n.type === 'string') fn(n)
  for (const k in n) if (k !== 'parent' && n[k] && typeof n[k] === 'object') walk(n[k], fn)
}

const used = new Map() // text -> {plural, where}
const dynamic = []
for (const p of files(root)) {
  if (p.includes('/locales/') || p.endsWith('/i18n.ts')) continue
  const sf = parse(p)
  walk(sf.ast, (n) => {
    if (n.type === 'CallExpression' && n.callee.type === 'Identifier' && ['t', 'tn', 'tNodes'].includes(n.callee.name)) {
      const where = `${relative(root, p)}:${sf.line(n.start)}`
      if (n.callee.name === 'tn') {
        const one = literal(n.arguments[1]), other = literal(n.arguments[2])
        if (one === null || other === null) dynamic.push(where); else used.set(other, { plural: true, one, where })
      } else {
        const s = literal(n.arguments[0])
        if (s === null) dynamic.push(where); else used.set(s, { plural: false, where })
      }
    }
  })
}

const catalog = new Map() // key -> {value, file}
const dupes = []
for (const p of files(join(root, 'locales/fr'))) {
  const sf = parse(p)
  walk(sf.ast, (n) => {
    if (n.type === 'Property') {
      const k = n.key.type === 'Identifier' ? n.key.name : literal(n.key)
      const v = literal(n.value) ?? (n.value.type === 'ArrayExpression' ? n.value.elements.map(literal) : null)
      if (k !== null) {
        if (catalog.has(k) && JSON.stringify(catalog.get(k).value) !== JSON.stringify(v)) dupes.push(`${k} (${catalog.get(k).file} / ${relative(root, p)})`)
        catalog.set(k, { value: v, file: relative(root, p) })
      }
    }
  })
}

const holes = (s) => [...String(s).matchAll(/\{\{(\w+)\}\}/g)].map((m) => m[1]).sort().join(',')
const missing = [], wrongShape = [], badHoles = []
for (const [k, u] of used) {
  const c = catalog.get(k)
  if (!c) { missing.push(`${u.where}  ${JSON.stringify(k)}`); continue }
  if (u.plural !== Array.isArray(c.value)) { wrongShape.push(`${u.where}  ${JSON.stringify(k)}`); continue }
  const vals = Array.isArray(c.value) ? c.value : [c.value]
  const want = holes(u.plural ? `${u.one} ${k}` : k).split(',').filter(Boolean)
  for (const v of vals) {
    const have = holes(v).split(',').filter(Boolean)
    const bad = have.filter((h) => !want.includes(h)).concat(u.plural ? [] : want.filter((h) => !have.includes(h)))
    if (bad.length) badHoles.push(`${u.where}  ${JSON.stringify(k)} → ${JSON.stringify(v)}`)
  }
}
const unused = [...catalog.keys()].filter((k) => !used.has(k))
const report = (title, list) => { if (list.length) console.log(`\n${title} (${list.length}):\n  ${list.slice(0, 60).join('\n  ')}`) }
console.log(`texts in code: ${used.size}; French entries: ${catalog.size}`)
report('MISSING French', missing)
report('Plural/singular mismatch', wrongShape)
report('Placeholder mismatch', badHoles)
report('Conflicting duplicates', dupes)
report('Calls with a non-literal text (check by hand)', dynamic)
report('Unused French entries', unused.map((k) => `${catalog.get(k).file}  ${JSON.stringify(k)}`))
process.exit(missing.length || wrongShape.length || badHoles.length || dupes.length ? 1 : 0)
