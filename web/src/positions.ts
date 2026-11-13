// Two kinds of position in a file, as distinct types so the compiler rejects using one for the other.
// Both count UTF-16 code units, but they count in different texts while suggesting: the editor then
// shows its own view of the file, with suggested changes applied (suggestmode.ts, suggestcore.ts).
//
// - SharedPos: in the shared text (Yjs), what comments, problems and collaborators refer to;
// - ViewPos: in what the editor shows (CodeMirror), what selections and clicks give.
//
// A plain number becomes one only by saying which (`sharedPos(n)`, `viewPos(n)`), which is where
// a mix-up would show; mapping between them is suggestcore's toView and toShared. Arithmetic on
// one (pos + 1) gives a plain number again, so the result has to be said which it is too.

export type SharedPos = number & { readonly __pos: 'shared' }
export type ViewPos = number & { readonly __pos: 'view' }

export const sharedPos = (n: number) => n as SharedPos
export const viewPos = (n: number) => n as ViewPos
