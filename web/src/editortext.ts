// How the editor holds a file's text, apart from everything else it does (editor.ts), so the
// property checks (scripts/check-props.mjs) test exactly what the editor uses.
//
// - Only "\n" breaks lines, as in the shared text. By default CodeMirror also takes "\r\n" (a file
//   saved on Windows) as one break: its positions then fell one short of the shared text's for
//   every line above, and typing landed that far off, in the middle of words (before 0.4.21).
// - Edits never cut a character outside the BMP (an emoji) in two (wholechars.ts).
import { EditorState } from '@codemirror/state'
import { wholeCharactersFilter } from './wholechars'

export const editorText = [EditorState.lineSeparator.of('\n'), wholeCharactersFilter]
