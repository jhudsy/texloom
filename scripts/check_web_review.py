#!/usr/bin/env python3
"""The web app's Review tab, in headless Chromium.

- Order: comments in a paper split across files are listed as the document reads (\\input,
  \\include and \\subfile from the main file), then files the document doesn't read, by name.
- Drafts: a second comment can be started while another is half-written, and each is saved
  with its own text on its own selection.
- Several at once (issues #34, #35): Resolve all resolves every open comment, and Undo reopens
  them all; ⌘/Ctrl-click and ⇧-click select cards; Select in text picks up the comments on the
  editor's selection; the bar's actions resolve and accept as one change, undone in one step.
- Mentions (issue #3): typing @ in a reply offers the project's collaborators, and Enter puts the
  name in without sending the reply; a comment naming you gets an @ count on the Review tab, which
  shows just those.

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


def add_comments(page, words: list[tuple[str, str]]) -> None:
    for f, word in words:
        comment_on(page, f, word)
        page.wait_for_selector(".rdraft textarea")
        page.keyboard.type(word.lower())
        page.keyboard.press(f"{MOD}+Enter")
        page.wait_for_timeout(250)
    page.wait_for_timeout(400)


def open_quotes(page) -> list[str]:
    return page.eval_on_selector_all(".review-list .rcard:not(.resolved) .rquote", "qs => qs.map(q => q.textContent.trim())")


def several(s: webcheck.Session) -> None:
    page = s.project(PAPER)
    add_comments(page, [("main.tex", "MAINSTART"), ("main.tex", "MAINMIDDLE"), ("main.tex", "MAINEND"), ("zintro.tex", "INTROONE")])
    s.check("four open comments to start with", len(open_quotes(page)) == 4, open_quotes(page))

    # Resolve all, then Undo
    page.once("dialog", lambda d: d.accept())
    page.get_by_role("button", name="Resolve all…").click()
    page.wait_for_timeout(400)
    s.check("Resolve all resolves every open comment", open_quotes(page) == [], open_quotes(page))
    page.get_by_role("button", name="Undo").click()
    page.wait_for_timeout(400)
    s.check("Undo reopens all of them, in one step", len(open_quotes(page)) == 4, open_quotes(page))

    # ⌘/Ctrl-click two cards, ⇧-click a range
    cards = page.locator(".review-list .rcard")
    cards.nth(0).locator(".rtext").click(modifiers=[MOD])
    cards.nth(2).locator(".rtext").click(modifiers=[MOD])
    picked = page.eval_on_selector_all(".rcard.selected .rquote", "qs => qs.map(q => q.textContent.trim())")
    s.check("⌘/Ctrl-click selects cards one by one", picked == ["MAINSTART", "MAINMIDDLE"], picked)
    cards.nth(3).locator(".rtext").click(modifiers=["Shift"])
    picked = page.eval_on_selector_all(".rcard.selected .rquote", "qs => qs.map(q => q.textContent.trim())")
    s.check("⇧-click selects from the last one clicked", picked == ["MAINSTART", "MAINMIDDLE", "MAINEND"], picked)
    s.check("the bar counts them", "3 selected" in page.locator(".rbulk").first.inner_text())
    page.get_by_role("button", name="Resolve 3 comments").click()
    page.wait_for_timeout(400)
    s.check("the bar's Resolve resolves just those", open_quotes(page) == ["INTROONE"], open_quotes(page))
    page.get_by_role("button", name="Undo").click()
    page.wait_for_timeout(400)
    s.check("and Undo brings all three back", len(open_quotes(page)) == 4, open_quotes(page))

    # Select in text: the comments on the editor's selection
    text = PAPER["main.tex"]
    select(page, "main.tex", text.index("MAINSTART"), text.index("MAINMIDDLE") + len("MAINMIDDLE"))
    page.get_by_role("button", name="Select in text").click()
    picked = page.eval_on_selector_all(".rcard.selected .rquote", "qs => qs.map(q => q.textContent.trim())")
    s.check("Select in text picks the comments on the selected text", picked == ["MAINSTART", "MAINMIDDLE"], picked)
    page.get_by_role("button", name="Clear").click()
    s.check("Clear empties the selection", page.locator(".rcard.selected").count() == 0)
    page.close()


def mentions(s: webcheck.Session) -> None:
    page = s.project(PAPER)  # as "Tester"
    # a collaborator the project knows: a personal link's name, as the owner granted it
    page.evaluate("() => window.__texloom.r.members.set('somekey', { name: 'Bob Jones', at: 0, by: 'Tester' })")
    add_comments(page, [("main.tex", "MAINSTART")])
    reply = page.locator(".review-list .rreply-in").first
    reply.click()
    page.keyboard.type("thanks @bo")
    page.wait_for_selector(".mention-menu", timeout=3000)
    offered = page.eval_on_selector_all(".mention-choice", "cs => cs.map(c => c.textContent)")
    s.check("typing @ offers the collaborators whose names start so", offered == ["@Bob Jones"], offered)
    page.keyboard.press("Enter")
    page.wait_for_timeout(300)
    s.check("Enter puts the name in, and doesn't send the reply", reply.input_value() == "thanks @Bob Jones " and page.locator(".rreply").count() == 0,
            (reply.input_value(), page.locator(".rreply").count()))
    page.keyboard.press("Enter")  # now it sends
    page.wait_for_timeout(400)
    s.check("the next Enter sends it", page.locator(".rreply").count() == 1)

    s.check("nothing mentions you yet: no @ on the Review tab", page.locator(".mention-badge").count() == 0)
    add_comments(page, [("main.tex", "MAINEND")])
    last = page.locator(".review-list .rreply-in").last
    last.click()
    page.keyboard.type("@Tester can you check this")
    page.keyboard.press("Escape")
    page.keyboard.press("Enter")
    page.wait_for_timeout(500)
    badge = page.locator(".mention-badge")
    s.check("a comment naming you puts @1 on the Review tab", badge.count() == 1 and badge.inner_text() == "@1", badge.all_inner_texts())
    s.check("its card is marked", page.locator(".rcard.mentions-me").count() == 1)
    badge.click()
    shown = page.eval_on_selector_all(".review-list .rquote", "qs => qs.map(q => q.textContent.trim())")
    s.check("the @ shows just the ones that mention you", shown == ["MAINEND"], shown)
    page.close()


if __name__ == "__main__":
    with webcheck.Session() as s:
        order(s)
        drafts(s)
        several(s)
        mentions(s)
    s.done()
