// Checks web/src/submission.ts, the arXiv / journal .zip: which files go in, comment stripping,
// merging into one .tex, and the main file's folder becoming the top level.
import { load, report } from './lib.mjs'

const [{ submission, needsBbl, stripTexComments }] = await load('src/submission.ts')

const bad = []
const eq = (what, got, want) => { if (JSON.stringify(got) !== JSON.stringify(want)) bad.push(`${what}:\n  got  ${JSON.stringify(got)}\n  want ${JSON.stringify(want)}`) }
const bin = new Uint8Array([1])
const keep = { stripComments: false, oneFile: false }
const zipped = (files, main, bbl = null, opts = keep) => submission(new Map(Object.entries(files)), main, bbl, opts)

// ---- which files go in ----
{
  const files = {
    'main.tex': '\\documentclass{article}\n\\usepackage{mystyle}\n\\graphicspath{{figs/}}\n\\begin{document}\n\\input{intro}\n\\include{chapters/two}\n\\includegraphics[width=1cm]{plot}\n% \\input{old}\n\\bibliographystyle{plain}\n\\bibliography{refs}\n\\end{document}\n',
    'mystyle.sty': '\\RequirePackage{helper}\n',
    'helper.sty': '',
    'intro.tex': 'Intro \\includegraphics{figs/photo.jpg}\n',
    'chapters/two.tex': 'Two\n',
    'figs/plot.pdf': bin, 'figs/photo.jpg': bin, 'figs/unused.png': bin,
    'old.tex': 'old\n', 'refs.bib': '@book{a,}', 'notes.txt': 'x',
    'main.aux': 'x', 'main.log': 'x', 'main.pdf': bin, 'main.synctex.gz': bin, 'main.bbl': 'old bbl',
    '00README.json': '{}',
  }
  const s = zipped(files, 'main.tex', 'BBL')
  eq('files in, with a .bbl', [...s.files.keys()].sort(), ['00README.json', 'chapters/two.tex', 'figs/photo.jpg', 'figs/plot.pdf', 'helper.sty', 'intro.tex', 'main.bbl', 'main.tex', 'mystyle.sty'])
  eq('the compiled .bbl, not the project’s', s.files.get('main.bbl'), 'BBL')
  eq('left out', s.leftOut, ['figs/unused.png', 'main.aux', 'main.log', 'main.pdf', 'main.synctex.gz', 'notes.txt', 'old.tex', 'refs.bib'])
  eq('without a .bbl, the .bib goes in', zipped(files, 'main.tex', null).files.has('refs.bib'), true)
  eq('needs a .bbl with \\bibliography', needsBbl(new Map(Object.entries(files)), 'main.tex'), true)
  eq('no .bbl needed with thebibliography', needsBbl(new Map([['m.tex', '\\begin{thebibliography}{1}\\end{thebibliography}\n% \\bibliography{x}']]), 'm.tex'), false)
  eq('biblatex needs one', needsBbl(new Map([['m.tex', '\\usepackage{biblatex}\n\\addbibresource{r.bib}']]), 'm.tex'), true)
  eq('a cycle of \\input ends', [...zipped({ 'a.tex': '\\input{b}', 'b.tex': '\\input{a}' }, 'a.tex').files.keys()].sort(), ['a.tex', 'b.tex'])
}

// ---- a main file in a folder ----
{
  const s = zipped({ 'paper/main.tex': '\\input{sec}\\includegraphics{img/a}', 'paper/sec.tex': 's', 'paper/img/a.png': bin, 'other/x.tex': '' }, 'paper/main.tex', 'B')
  eq('rebased to the main file’s folder', [s.main, [...s.files.keys()].sort()], ['main.tex', ['img/a.png', 'main.bbl', 'main.tex', 'sec.tex']])
  const t = zipped({ 'paper/main.tex': '\\includegraphics{../shared/a}', 'shared/a.pdf': bin }, 'paper/main.tex')
  eq('kept as it is when a file is outside', [t.main, [...t.files.keys()].sort()], ['paper/main.tex', ['paper/main.tex', 'shared/a.pdf']])
}

// ---- comments ----
const strip = (s) => stripTexComments(s)
eq('a comment-only line goes', strip('a\n% gone\n  % gone too\nb'), 'a\nb')
eq('a trailing comment leaves %', strip('text % note\nx'), 'text %\nx')
eq('\\% is a percent sign', strip('50\\% off % note'), '50\\% off %')
eq('\\\\% is a line break then a comment', strip('a\\\\% note'), 'a\\\\%')
eq('\\verb keeps its %', strip('\\verb|50%| and % note'), '\\verb|50%| and %')
eq('\\url keeps its %', strip('\\url{a%20b} % note'), '\\url{a%20b} %')
eq('\\href keeps its %', strip('\\href{http://x/%41}{y} % note'), '\\href{http://x/%41}{y} %')
eq('verbatim is left alone', strip('\\begin{verbatim}\n% kept\n\\end{verbatim}\n% gone'), '\\begin{verbatim}\n% kept\n\\end{verbatim}')
eq('lstlisting is left alone', strip('\\begin{lstlisting}\nx = 1 % kept\n\\end{lstlisting}'), '\\begin{lstlisting}\nx = 1 % kept\n\\end{lstlisting}')
eq('a comment environment goes', strip('a\n\\begin{comment}\nsecret\n\\end{comment}\nb'), 'a\nb')
eq('blank lines stay (paragraphs)', strip('a\n\n% c\n\nb'), 'a\n\n\nb')
{
  const s = zipped({ 'm.tex': '\\usepackage{p}\nx % c\n', 'p.sty': '% kept in packages\n' }, 'm.tex', null, { stripComments: true, oneFile: false })
  eq('only .tex files are stripped', [s.files.get('m.tex'), s.files.get('p.sty')], ['\\usepackage{p}\nx %\n', '% kept in packages\n'])
}

// ---- one file ----
{
  const files = {
    'main.tex': '\\documentclass{article}\n\\begin{document}\n\\input{a}\n% \\input{a}\n\\include{b}\n\\bibliography{refs}\n\\end{document}\n',
    'a.tex': 'A text', 'b.tex': 'B text\n', 'refs.bib': '',
  }
  const s = zipped(files, 'main.tex', '\\begin{thebibliography}{1}\\end{thebibliography}\n', { stripComments: false, oneFile: true })
  eq('merged, with the BibTeX .bbl inlined', [...s.files.keys()], ['main.tex'])
  eq('merged text', s.files.get('main.tex'), '\\documentclass{article}\n\\begin{document}\nA text\n\n% \\input{a}\n\\clearpage\nB text\n\\clearpage\n\n\\begin{thebibliography}{1}\\end{thebibliography}\n\n\\end{document}\n')
  eq('merged files listed', s.merged, ['a.tex', 'b.tex'])
  const bl = zipped({ 'main.tex': '\\usepackage{biblatex}\n\\addbibresource{refs.bib}\n\\input{a}\n\\printbibliography', 'a.tex': 'A', 'refs.bib': '' }, 'main.tex', 'BBL', { stripComments: false, oneFile: true })
  eq('biblatex keeps its .bbl', [...bl.files.keys()].sort(), ['main.bbl', 'main.tex'])
  const sub = zipped({ 'main.tex': '\\subfile{s}', 's.tex': '\\documentclass[main]{subfiles}\n\\begin{document}\nS\n\\end{document}\n' }, 'main.tex', null, { stripComments: false, oneFile: true })
  eq('a subfile’s body is merged', sub.files.get('main.tex'), '\nS\n')
}

report('submission', bad, 'submission: all cases agree')
