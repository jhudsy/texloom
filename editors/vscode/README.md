# TeXLoom comments for VS Code

**This extension needs TeXLoom itself on your computer:** the [desktop app](https://texloom.org) or the `texloom` command-line helper. It only shows comments; it doesn't sync or store anything.

The comments and suggested changes of a [TeXLoom](https://texloom.org) project, beside the text they are about, in the folder you have open: as comment threads like a pull request review, with **reply** and **resolve**. A comment that mentions you (`@yourname`) is open when you arrive.

It needs a TeXLoom folder (one the desktop app or `texloom run` keeps in step with a project). It starts `texloom lsp`, TeXLoom's language server, so the same comments also work in Neovim, Emacs, Helix, Zed and Sublime Text (see [the TeXLoom README](https://github.com/jhudsy/texloom#readme)).

Settings: `texloom.command` (where TeXLoom is, if not on your PATH), `texloom.comments` (threads, Problems panel entries, or both).

Suggested changes are shown, but accepted or rejected in the web app's Review tab, where the change can be seen. To start a comment, click the **+** beside a line, or select text and use **Add Comment** (⌘K ⌘⌥C, or Ctrl+K Ctrl+Alt+C): it goes on the selected text, or on the line's text when nothing is selected. Save first if you've just typed that text: a comment can only go on text the project already has.
