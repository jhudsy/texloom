// Checks src/pdfanchor.ts: text selected in a PDF must be found in its LaTeX source, near the
// lines SyncTeX names, despite commands, accents, comments, hyphenation and ligatures; text that
// isn't in the source must fall back to those lines; a bibliography entry must be found in the .bib.
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { build } from 'vite'

const web = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const out = path.join(web, 'node_modules/.cache/check-pdfanchor')
await build({ configFile: false, root: web, logLevel: 'warn', build: { ssr: 'src/pdfanchor.ts', outDir: out, emptyOutDir: true, minify: false, rollupOptions: { output: { entryFileNames: 'pdfanchor.mjs' } } } })
const { anchorInSource, findAnywhere } = await import(pathToFileURL(path.join(out, 'pdfanchor.mjs')).href)

const src = `\\section{Intro}\\label{sec:intro}
% a comment that is not in the PDF: quick brown fox
The \\textbf{quick} brown fox, as Knuth~\\cite{knuth} said,
jumps over the \\emph{lazy} dog in Section~\\ref{sec:end}.
Caf\\'e na\\"{\\i}ve \\c{c}a and Stra\\ss e are spelt with macros.
A well-known over-crediting problem; continuous effort.
The equation $E = mc^2$ holds.
\\begin{tabular}{ll}
alpha & beta \\\\
gamma & delta \\\\
\\end{tabular}
`
const lineOf = (s) => src.split('\n').findIndex((l) => l.includes(s)) + 1
let failed = 0
const check = (name, ok, detail) => { if (!ok) { failed++; console.log(`FAIL ${name}: ${detail}`) } }
const cases = [
  // [what the PDF selection says, the line SyncTeX gives (first, last), the source text expected, exact?]
  ['quick brown fox, as Knuth [1] said,', 'The \\textbf', 'The \\textbf', 'quick} brown fox, as Knuth~\\cite{knuth} said', true],
  ['uick brown fo', 'The \\textbf', 'The \\textbf', 'quick} brown fox', true], // starts and ends inside words
  ['jumps over the lazy dog in Section 2.', 'jumps', 'jumps', 'jumps over the \\emph{lazy} dog in Section', true],
  ['Café naïve ça and Straße', 'Caf', 'Caf', "Caf\\'e na\\\"{\\i}ve \\c{c}a and Stra\\ss e", true],
  ['well-known over-\ncrediting problem; contin-\nuous effort', 'over-crediting', 'over-crediting', 'well-known over-crediting problem; continuous effort', true],
  ['the ﬁrst ﬂow', 'over-crediting', 'over-crediting', null, false], // not in the source: the line
  ['alpha beta gamma', 'end{tabular}', 'end{tabular}', 'alpha & beta \\\\\ngamma', true], // SyncTeX names the end of the table
]
for (const [sel, a, b, want, exact] of cases) {
  const r = anchorInSource(src, lineOf(a), lineOf(b), sel)
  const got = src.slice(r.from, r.to)
  check(JSON.stringify(sel), r.exact === exact && (want == null || got === want), `got ${JSON.stringify(got)} (exact ${r.exact})`)
  if (!exact) check(`${JSON.stringify(sel)} falls back to its line`, got === src.split('\n')[lineOf(a) - 1].trim(), JSON.stringify(got))
}
const bib = `@article{knuth,\n  author = {Donald E. Knuth},\n  title = {The {\\TeX}book},\n  journal = {Computers and Typesetting},\n  year = 1984\n}\n`
const f = findAnywhere([{ name: 'main.tex', text: src }, { name: 'refs.bib', text: bib }], 'Donald E. Knuth. The TeXbook. Computers and Typesetting, 1984.')
check('bibliography entry in the .bib', f?.file === 'refs.bib' && bib.slice(f.from, f.to).includes('Computers and Typesetting'), JSON.stringify(f))
check('a word or two is not looked for everywhere', findAnywhere([{ name: 'main.tex', text: src }], 'brown fox') === null, 'found')
if (failed) { console.log(`pdfanchor: ${failed} failed`); process.exit(1) }
console.log(`pdfanchor: all ${cases.length + 2} cases found where they should be`)
