// Checks each language's catalogue against the code: every t()/tn()/tNodes() text needs an entry,
// counts need the language's plural forms, and placeholders must match.
// Run: npm run check-i18n [-- de pl] [-- --allow-missing]
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

// Languages: every folder in src/locales (or the ones named: node scripts/check-i18n.mjs de pl).
// --allow-missing: report untranslated text without failing (for a translation in progress).
const args = process.argv.slice(2)
const allowMissing = args.includes('--allow-missing')
const named = args.filter((a) => !a.startsWith('--'))
const langs = named.length ? named : readdirSync(join(root, 'locales')).filter((d) => statSync(join(root, 'locales', d)).isDirectory()).sort()
const declared = [...readFileSync(join(root, 'i18n.ts'), 'utf8').matchAll(/\{\s*id:\s*'(\w+)'/g)].map((m) => m[1])

const holes = (s) => [...String(s).matchAll(/\{\{(\w+)\}\}/g)].map((m) => m[1]).sort().join(',')
const report = (title, list) => { if (list.length) console.log(`\n${title} (${list.length}):\n  ${list.slice(0, 60).join('\n  ')}`) }
let failed = false
console.log(`texts in code: ${used.size}`)
report('Calls with a non-literal text (check by hand)', dynamic)

for (const lang of langs) {
  const dir = join(root, 'locales', lang)
  // the forms a count needs, e.g. pl: one, few, many, other. French, Spanish, Italian,
  // Portuguese and Catalan have "many" only for round millions ("1 million de fichiers"),
  // where the "other" form serves, so it isn't required there.
  const LARGE_MANY = ['fr', 'es', 'it', 'pt', 'ca']
  const categories = new Intl.PluralRules(lang).resolvedOptions().pluralCategories
    .filter((c) => !(c === 'many' && LARGE_MANY.includes(lang.split('-')[0])))
  const catalog = new Map() // key -> {value, file}
  const dupes = []
  for (const p of files(dir)) {
    const sf = parse(p)
    const inner = new Set() // the forms inside a count's { one, few, … } entry
    walk(sf.ast, (n) => {
      if (n.type !== 'Property' || inner.has(n)) return
      const k = n.key.type === 'Identifier' ? n.key.name : literal(n.key)
      if (k === null) return
      let v = literal(n.value)
      if (v === null && n.value.type === 'ArrayExpression') v = n.value.elements.map(literal)
      if (v === null && n.value.type === 'ObjectExpression') {
        for (const q of n.value.properties) inner.add(q)
        v = Object.fromEntries(n.value.properties.map((q) => [q.key.type === 'Identifier' ? q.key.name : literal(q.key), literal(q.value)]))
      }
      if (v === null) return
      if (catalog.has(k) && JSON.stringify(catalog.get(k).value) !== JSON.stringify(v)) dupes.push(`${k} (${catalog.get(k).file} / ${relative(root, p)})`)
      catalog.set(k, { value: v, file: relative(root, p) })
    })
  }
  const missing = [], wrongShape = [], badHoles = []
  for (const [k, u] of used) {
    const c = catalog.get(k)
    if (!c) { missing.push(`${u.where}  ${JSON.stringify(k)}`); continue }
    const v = c.value, isPlural = typeof v !== 'string'
    if (u.plural !== isPlural) { wrongShape.push(`${u.where}  ${JSON.stringify(k)}: ${u.plural ? 'a count needs plural forms' : 'not a count: give one text'}`); continue }
    let forms = [v]
    if (Array.isArray(v)) {
      forms = v
      if (v.length !== 2) wrongShape.push(`${u.where}  ${JSON.stringify(k)}: [one, other] needs exactly two forms`)
      else if (categories.some((c) => c !== 'one' && c !== 'other')) wrongShape.push(`${u.where}  ${JSON.stringify(k)}: ${lang} has the forms ${categories.join(', ')}: write { ${categories.map((c) => `${c}: …`).join(', ')} }`)
    } else if (isPlural) {
      forms = Object.values(v)
      const lacking = categories.filter((c) => !(c in v))
      const extra = Object.keys(v).filter((c) => !new Intl.PluralRules(lang).resolvedOptions().pluralCategories.includes(c))
      if (lacking.length || extra.length) wrongShape.push(`${u.where}  ${JSON.stringify(k)}: ${lang} needs ${categories.join(', ')}${extra.length ? ` (not ${extra.join(', ')})` : ''}`)
    }
    const want = holes(u.plural ? `${u.one} ${k}` : k).split(',').filter(Boolean)
    for (const f of forms) {
      const have = holes(f).split(',').filter(Boolean)
      const bad = have.filter((h) => !want.includes(h)).concat(u.plural ? [] : want.filter((h) => !have.includes(h)))
      if (bad.length) badHoles.push(`${u.where}  ${JSON.stringify(k)} → ${JSON.stringify(f)}`)
    }
  }
  const unused = [...catalog.keys()].filter((k) => !used.has(k))
  console.log(`\n== ${lang}: ${used.size - missing.length} of ${used.size} translated (${categories.join('/')} plurals)${declared.includes(lang) ? '' : ` — not in LANGUAGES in i18n.ts, so nobody can choose it yet`}`)
  report(`Missing ${lang}`, missing)
  report('Plural forms wrong', wrongShape)
  report('Placeholder mismatch', badHoles)
  report('Conflicting duplicates', dupes)
  report('Unused entries', unused.map((k) => `${catalog.get(k).file}  ${JSON.stringify(k)}`))
  if ((missing.length && !allowMissing) || wrongShape.length || badHoles.length || dupes.length) failed = true
}
process.exit(failed ? 1 : 0)
