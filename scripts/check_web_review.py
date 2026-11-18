#!/usr/bin/env python3
"""The web app's Review tab, in headless Chromium.

- Order: comments in a paper split across files are listed as the document reads (\\input,
  \\include and \\subfile from the main file), then files the document doesn't read, by name.
- Drafts: a second comment can be started while another is half-written, and each is saved
  with its own text on its own selection.

    cd web && npm run build && cd ..
    uv run --extra server --with playwright python scripts/check_web_review.py
"""

from __future__ import annotations

import webcheck
from webcheck import MOD, select

PAPER = {
    "main.tex": "\\documentclass{article}\n\\begin{document}\nMAINSTART text.\n\\input{zintro}\nMAINMIDDLE text.\n\\input{appendix}\nMAINEND text.\n\\end{document}\n",
    "zintro.tex": "INTROONE here.\nINTROTWO there.\n",
    "appendix.tex": "APPENDIXONE text.\n",
    "notes.tex": "UNUSED file.\n",
}


def comment_on(page, file: str, word: str) -> None:
    i = PAPER[file].index(word)
    select(page, file, i, i + len(word))
    page.keyboard.press(f"{MOD}+Alt+m")


def order(s: webcheck.Session) -> None:
    page = s.project(PAPER)
    jumbled = [("notes.tex", "UNUSED"), ("appendix.tex", "APPENDIXONE"), ("main.tex", "MAINEND"), ("zintro.tex", "INTROTWO"),
               ("main.tex", "MAINSTART"), ("zintro.tex", "INTROONE"), ("main.tex", "MAINMIDDLE")]
    for f, word in jumbled:
        comment_on(page, f, word)
        page.wait_for_selector(".rdraft textarea")
        page.keyboard.type(word)
        page.keyboard.press(f"{MOD}+Enter")
        page.wait_for_timeout(300)
    page.wait_for_timeout(500)
    got = page.eval_on_selector_all(".review-list .rquote", "qs => qs.map(q => q.textContent.trim())")
    want = ["MAINSTART", "INTROONE", "INTROTWO", "MAINMIDDLE", "APPENDIXONE", "MAINEND", "UNUSED"]
    s.check("comments are listed in reading order", got == want, f"got {got}")
    page.close()


def drafts(s: webcheck.Session) -> None:
    page = s.project(PAPER)
    comment_on(page, "main.tex", "MAINSTART")
    page.wait_for_selector(".rdraft textarea")
    page.keyboard.type("first, half-written")
    comment_on(page, "main.tex", "MAINEND")  # a second one while the first is open
    page.wait_for_function("() => document.querySelectorAll('.rdraft textarea').length === 2", timeout=5000)
    areas = page.locator(".rdraft textarea")
    s.check("a second draft opens beside the first", areas.count() == 2)
    s.check("the first draft keeps its text", "first, half-written" in [areas.nth(i).input_value() for i in range(areas.count())])
    focused = page.evaluate("() => document.activeElement?.closest('.rdraft') ? document.activeElement.value : null")
    s.check("the new draft has the focus, empty", focused == "")
    page.keyboard.type("second")
    page.keyboard.press(f"{MOD}+Enter")
    page.wait_for_timeout(300)
    first = page.locator(".rdraft textarea").first
    first.focus()
    page.keyboard.press(f"{MOD}+Enter")
    page.wait_for_timeout(500)
    pairs = page.eval_on_selector_all(".review-list .rquote", "qs => qs.map(q => [q.textContent.trim(), q.nextElementSibling?.textContent.trim()])")
    s.check("both are saved, each on its own selection with its own text",
            pairs == [["MAINSTART", "first, half-written"], ["MAINEND", "second"]], f"got {pairs}")
    s.check("no drafts are left open", page.locator(".rdraft textarea").count() == 0)
    page.close()


if __name__ == "__main__":
    with webcheck.Session() as s:
        order(s)
        drafts(s)
    s.done()
