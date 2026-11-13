// Checks of what must always hold, run only in test builds: on this computer's own addresses when an
// end-to-end check turns them on (scripts/check_synconly.py sets texloom.testHooks). A broken one is
// recorded on window.__texloomBroken and logged; the check fails on any. They cost a full copy of
// the text per check, which is why they don't run for real users.

let on: boolean | null = null
export function checking(): boolean {
  if (on === null) {
    try { on = /^(localhost|127\.0\.0\.1|\[::1\])$/.test(location.hostname) && localStorage.getItem('texloom.testHooks') === '1' } catch { on = false }
  }
  return on
}

export function broken(what: string, detail: unknown = '') {
  const w = window as any
  ;(w.__texloomBroken ??= []).push(`${what}: ${typeof detail === 'string' ? detail : JSON.stringify(detail)}`.slice(0, 2000))
  console.error('texloom invariant broken:', what, detail)
}

const LONE = /[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/

/** Text the project may hold: no "\r" (line breaks are "\n" only) and no half characters. */
export function wellFormed(where: string, s: string) {
  if (s.includes('\r')) broken(`${where} has \\r`, s.slice(Math.max(0, s.indexOf('\r') - 40), s.indexOf('\r') + 40))
  const m = LONE.exec(s)
  if (m) broken(`${where} has half a character`, s.slice(Math.max(0, m.index - 20), m.index + 20))
}

/** Where the first difference is, for a readable report. */
export function firstDifference(a: string, b: string): string {
  let i = 0
  while (i < a.length && i < b.length && a[i] === b[i]) i++
  return `at ${i}: ${JSON.stringify(a.slice(Math.max(0, i - 30), i + 30))} vs ${JSON.stringify(b.slice(Math.max(0, i - 30), i + 30))}`
}
