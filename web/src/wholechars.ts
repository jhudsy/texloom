// Edits never cut a character outside the BMP (an emoji, say) in two. In the editor such a
// character is two UTF-16 units; an edit between them leaves half a character, which Yjs turns
// into "�" for everyone and the desktop app's copy can't hold at all, so the copies differ.
// Typing, deleting and pasting never do this (CodeMirror moves by whole characters), but other
// edits can (an extension, an input method, the assistant): they're widened to whole characters.
import { EditorState, type ChangeSpec, type Text, type Transaction } from '@codemirror/state'
import { ySyncAnnotation } from 'y-codemirror.next'
import { isHigh, isLow } from './diff'

const code = (doc: Text, at: number) => doc.sliceString(at, at + 1).charCodeAt(0)
/** Whether `at` is between the two halves of a character in `doc`. */
const cuts = (doc: Text, at: number) => at > 0 && at < doc.length && isHigh(code(doc, at - 1)) && isLow(code(doc, at))
const LONE = /[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/g
const LONE1 = new RegExp(LONE.source) // (a /g regex keeps its place between tests)

/** The same changes on whole characters. What's left of a character one cut through is put back
 *  with the change (so changing only its second half still gives a whole character); a half left
 *  on its own goes, so deleting half a character deletes it, as does inserting one. */
export function wholeCharacters(tr: Transaction): Transaction | { changes: ChangeSpec[]; effects: Transaction['effects']; annotations: never[]; userEvent?: string; scrollIntoView: boolean } {
  if (!tr.docChanged || tr.annotation(ySyncAnnotation)) return tr // from the shared text: already as it is there
  const doc = tr.startState.doc
  let cut = false
  tr.changes.iterChanges((from, to, _fb, _tb, inserted) => { if (cuts(doc, from) || cuts(doc, to) || LONE1.test(inserted.toString())) cut = true })
  if (!cut) return tr
  const changes: { from: number; to: number; insert: string }[] = []
  tr.changes.iterChanges((from, to, _fb, _tb, inserted) => {
    const a = cuts(doc, from) ? from - 1 : from, b = cuts(doc, to) ? to + 1 : to
    changes.push({ from: a, to: b, insert: (doc.sliceString(a, from) + inserted.toString() + doc.sliceString(to, b)).replace(LONE, '') })
  })
  if (changes.some((c, i) => i > 0 && c.from < changes[i - 1].to)) return tr // widened into each other: rare, left as it was
  const userEvent = (['input.type', 'input.paste', 'input', 'delete', 'undo', 'redo', 'move'] as const).find((e) => tr.isUserEvent(e))
  return { changes, effects: tr.effects, annotations: [], userEvent, scrollIntoView: tr.scrollIntoView }
}

export const wholeCharactersFilter = EditorState.transactionFilter.of(wholeCharacters)
