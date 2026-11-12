// The name shown to collaborators. It follows the project list to this person's other browsers
// (listsync.ts), so it carries when it was set: the latest one wins.
export const NAME_CHANGED = 'texloom-name-changed'

export const myName = {
  get(): string { try { return localStorage.getItem('texloom.name') || '' } catch { return '' } },
  /** When it was set here (0: before this was recorded). */
  at(): number { try { return Number(localStorage.getItem('texloom.nameAt')) || 0 } catch { return 0 } },
  set(v: string, at = Date.now(), quietly = false) {
    try { localStorage.setItem('texloom.name', v); localStorage.setItem('texloom.nameAt', String(at)) } catch {}
    if (!quietly) window.dispatchEvent(new Event(NAME_CHANGED))
  },
}
