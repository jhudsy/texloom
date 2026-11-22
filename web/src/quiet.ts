// Errors deliberately not shown to the person (issue #24): browser storage blocked or full, a
// folder that has gone, a best-effort cleanup, a peer that has left. Each says why it's safe to
// ignore, and none disappears without a trace: the last few are kept, and with debugging on
// (localStorage texloom.debug = 1) each goes to the console as it happens. scripts/check_quiet.py
// refuses an empty `catch` or a no-op `.catch()`, so every such error comes through here.

const KEEP = 100
const recent: { at: string; why: string; error: unknown }[] = []
let debugging: boolean | undefined

/** The reason most often given: reading or writing browser storage, blocked (private browsing,
 *  site settings) or full. Whatever was being kept is then kept for this visit only, or not at all. */
export const STORAGE = 'browser storage blocked or full'

/** An error that is safe to ignore here, and why. Returns undefined, so `.catch((e) => quiet(why, e))`
 *  stands in for `.catch(() => undefined)`. */
export function quiet(why: string, error?: unknown): undefined {
  recent.push({ at: new Date().toISOString(), why, error })
  if (recent.length > KEEP) recent.shift()
  if (debugging === undefined) {
    try { debugging = globalThis.localStorage?.getItem('texloom.debug') === '1' } catch { debugging = false } // (no storage: no debugging)
  }
  if (debugging) console.debug(`[texloom] ignored: ${why}`, error)
  return undefined
}

/** The errors ignored lately, oldest first (window.texloom.quiet() with debugging on). */
export const quieted = () => recent.slice()
