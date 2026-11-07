// Interface translations. The English text is the key: t('Share') shows the current
// language's version of "Share", or the English when there is none (so text added later
// shows in English until it is translated). Placeholders are {{name}}, so LaTeX braces in
// messages ("\label{…}") are never mistaken for them.
//
// The language is fixed for the page's life: chosen in Settings, else the browser's.

export type Lang = 'en' | 'fr'
/** A plain text, or a count's forms: [one, other] for languages with two (English, French,
 *  German, Spanish…), or one per plural category the language uses (Intl.PluralRules:
 *  zero, one, two, few, many, other), e.g. Polish { one, few, many, other }. */
type Entry = string | [one: string, other: string] | ({ other: string } & Partial<Record<Intl.LDMLPluralRule, string>>)
export type Catalog = Record<string, Entry>

const LANG_KEY = 'texloom.lang'
export const LANGUAGES: { id: Lang; name: string }[] = [{ id: 'en', name: 'English' }, { id: 'fr', name: 'Français' }]

/** The chosen language ('auto': the browser's). */
export function langPref(): Lang | 'auto' {
  try { const v = localStorage.getItem(LANG_KEY); if (LANGUAGES.some((l) => l.id === v)) return v as Lang } catch {}
  return 'auto'
}
export function setLangPref(v: Lang | 'auto') {
  try { if (v === 'auto') localStorage.removeItem(LANG_KEY); else localStorage.setItem(LANG_KEY, v) } catch {}
}

function detect(): Lang {
  const p = langPref()
  if (p !== 'auto') return p
  for (const l of (typeof navigator !== 'undefined' ? navigator.languages ?? [navigator.language] : [])) {
    const base = String(l).toLowerCase().split('-')[0]
    const known = LANGUAGES.find((x) => x.id === base)
    if (known) return known.id
  }
  return 'en'
}

export const lang: Lang = detect()
/** For dates and numbers: the chosen language, or (on automatic) the browser's own locale. */
export const locale: string | undefined = langPref() === 'auto' ? undefined : lang
if (typeof document !== 'undefined') document.documentElement.lang = lang

// Each language's text lives in locales/<lang>/<module>.ts (one file per source file).
const modules = import.meta.glob<{ default: Catalog }>('./locales/*/*.ts', { eager: true })
const catalog: Catalog = Object.assign({}, ...Object.entries(modules).filter(([p]) => p.startsWith(`./locales/${lang}/`)).map(([, m]) => m.default))
const plural = new Intl.PluralRules(lang)

const fill = (s: string, vars?: Record<string, unknown>) =>
  vars ? s.replace(/\{\{(\w+)\}\}/g, (m, k: string) => (k in vars ? String(vars[k]) : m)) : s

/** The interface text `en` in the current language, with {{name}} placeholders filled. */
export function t(en: string, vars?: Record<string, unknown>): string {
  const tr = catalog[en]
  return fill(typeof tr === 'string' ? tr : en, vars)
}

/** A count: tn(n, '{{n}} file', '{{n}} files'). The catalogue keys it by the English plural
 *  form; the language's own rules pick the form (French counts 0 and 1 as "one"). */
export function tn(n: number, one: string, other: string, vars?: Record<string, unknown>): string {
  const v = { n, ...vars }
  const tr = catalog[other]
  const cat = plural.select(n)
  if (Array.isArray(tr)) return fill(cat === 'one' ? tr[0] : tr[1], v)
  if (tr && typeof tr === 'object') return fill(tr[cat] ?? tr.other, v)
  return fill(n === 1 ? one : other, v)
}

/** Like t(), for a sentence with elements inside it (a link, a bold name): the placeholders
 *  become those nodes, in whatever order the translation puts them. */
export function tNodes(en: string, vars: Record<string, Node | string>): (Node | string)[] {
  const tr = catalog[en]
  const s = typeof tr === 'string' ? tr : en
  const out: (Node | string)[] = []
  let at = 0
  for (const m of s.matchAll(/\{\{(\w+)\}\}/g)) {
    if (m.index! > at) out.push(s.slice(at, m.index))
    out.push(m[1] in vars ? vars[m[1]] : m[0])
    at = m.index! + m[0].length
  }
  if (at < s.length) out.push(s.slice(at))
  return out
}

/** Every key the catalogue has (for the completeness check in development). */
export const catalogKeys = () => Object.keys(catalog)
