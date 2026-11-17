# Changes PDF with latexdiff in the browser: where this stopped

Issue #4. Parked on this branch; `main` doesn't have it.

## The plan

- In History, a selected version gets **PDF of changes…**: from that version to now, or to another
  version (named ones first).
- The PDF opens in a viewer in the app, with **Download PDF** and **Download .tex** (the marked-up
  source, for journals that want it, or to fix by hand if it doesn't compile). It's never stored in
  the project or sent to collaborators.
- How it's made: History rebuilds the old version's files; latexdiff (`--flatten`, following
  `\input`/`\include`) compares the two versions; the result compiles in the browser with the newer
  version's pictures and bibliography.

## What's here

- `web/public/vendor/latexdiff.pl`: latexdiff 1.4.0, the stand-alone `latexdiff-so` (GPL-3, with
  Algorithm::Diff 1.15 inlined), unchanged, from https://ctan.org/pkg/latexdiff.
- `web/public/latexdiff-worker.js`: runs it in the Perl the in-browser TeX engine ships for Biber
  (`biber.js`), as `texcount-worker.js` runs TeXCount.
- The licence entry in `web/vite.config.ts`.

## Why it stopped

latexdiff loads and runs `--version` in that Perl (5.38.2; every module it uses is there), but
crashes ("memory access out of bounds") as soon as it compares anything, even its own example
files. Its regular expressions nest deeply (`$pat_n`: braces 20 levels deep), and compiling and
matching them overflows the WebAssembly Perl's stack, which is fixed when BusyTeX builds it.
Lowering the depth to 4 lets a trivial document through; real ones still crash.

## Ways to pick it up

1. Our own LaTeX-aware diff in TypeScript: prose word by word, commands kept, changed maths, tables
   and figures marked as whole blocks (latexdiff's safe mode). About 2–3 days.
2. A Perl built for WebAssembly with a larger stack, if one exists or can be built.
3. A zip of both versions (each merged into one .tex) to run latexdiff locally.
