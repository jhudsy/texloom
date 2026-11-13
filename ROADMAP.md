# Roadmap

What's planned or considered, roughly in the order it would be done. Each item is a GitHub issue,
which has the details and is where it's discussed; this file is the overview. Known limitations
that are accepted rather than planned are in [DESIGN.md](DESIGN.md#known-limitations--open-questions).

## Next

1. **[Report the pycrdt sticky-index bug upstream](https://github.com/jhudsy/texloom/issues/1)**, then
   drop our workarounds once it's fixed. About 15 minutes to report. pycrdt misplaces anchors in
   non-ASCII text, so the desktop app avoids them there (`texloom/anchors.py`).
2. **[arXiv / journal-ready export](https://github.com/jhudsy/texloom/issues/2)**: a `.zip` with the
   `.bbl`, without unused or build files, comments optionally stripped, optionally one `.tex`.
   About a day; the most useful feature for the effort.

## Later

3. **[Tell collaborators what's new](https://github.com/jhudsy/texloom/issues/3)**: unread counts,
   changes since your last visit, `@name` mentions. 2–4 days; only while TeXLoom is open, since the
   server can't read projects.
4. **[A PDF of the changes between two versions](https://github.com/jhudsy/texloom/issues/4)**, like
   `latexdiff`. 2–3 days; builds on the export's merging into one file.

## Testing

5. **[Deterministic simulation tests for the folder sync](https://github.com/jhudsy/texloom/issues/7)**:
   a simulated filesystem and clock, so timing bugs fail every time. 2–3 days; before the folder
   sync next changes significantly.
6. **[TLA+ model of the GitHub lease](https://github.com/jhudsy/texloom/issues/8)**, and later the
   server protocol. 2–3 days each; once several people use GitHub sync.

What's checked already: README → Checks, and DESIGN.md → Guarding text handling.

## Parked

Decided to wait; each says when to revisit it.

- **[LanguageTool grammar checking](https://github.com/jhudsy/texloom/issues/5)**: sends text to a
  third party, so opt-in only; if people ask.
- **[Mendeley](https://github.com/jhudsy/texloom/issues/6)**: needs a server-side secret; exporting a
  `.bib` into a synced folder works today; if people ask.
- **[Server files in object storage](https://github.com/jhudsy/texloom/issues/9)** (S3, R2): when the
  server's disk gets tight.
- **[A fault-tolerant, then load-balanced relay](https://github.com/jhudsy/texloom/issues/12)**: first
  a warm standby (the database streamed with Litestream, a second server ready to take over), later
  several servers at once (each project's changes numbered by one writer, live changes shared between
  servers). When downtime starts to matter, or one server gets busy; object storage comes first.
- **[One-click desktop updates](https://github.com/jhudsy/texloom/issues/10)**: once the apps are
  signed.
- **[Direct browser-to-browser sync (WebRTC)](https://github.com/jhudsy/texloom/issues/11)**: small
  benefit for a large cost.
