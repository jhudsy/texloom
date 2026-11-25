#!/usr/bin/env python3
"""When the web app downloads a compiled PDF (issue 15), in headless Chromium.

A collaborator's compile reaches every browser with the project open as a small note ("the latest
output is these files"); what costs bandwidth is each browser then fetching the PDF. A browser
fetches it only while it is showing the PDF panel (or has the PDF popped out), and only the newest:

- showing the PDF: a new compile is fetched once, and shown;
- another tab (the Assistant), or the panel closed: the new compile's log is read (the editor marks its
  problems) but not its PDF; coming back to the PDF fetches the newest, once;
- several compiles meanwhile: only the last one is fetched;
- popped out into its own window: the PDF is fetched for that window, wherever the main window is.

The compiling side is a second browser profile; it already holds what it makes, so it fetches nothing.

    cd web && npm run build && cd ..
    uv run --extra server --with playwright python scripts/check_web_pdf.py
"""

from __future__ import annotations

import webcheck

FILES = {"main.tex": "\\documentclass{article}\n\\begin{document}\nHello.\n\\end{document}\n"}


def pdf(text: str) -> list[int]:
    """A small, valid one-page PDF showing `text` (different text, different file)."""
    stream = f"BT /F1 18 Tf 20 50 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 240 100] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out = b"%PDF-1.4\n"
    at = []
    for i, o in enumerate(objs, 1):
        at.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    x = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % a for a in at)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, x)
    return list(out)


def compile_as(page, text: str) -> tuple[str, str]:
    """What a collaborator's compile does: upload the PDF and its log, then say they are the latest. Returns their ids."""
    ids = page.evaluate("""async ([bytes, text]) => {
      const r = __texloom.r
      const pdf = await r.putBlob(new Uint8Array(bytes), 'output')
      const log = await r.putBlob(new TextEncoder().encode('This is a log: ' + text), 'output')
      r.out.set('latest', { pdf, log, ok: true, by: 'Compiler', at: Date.now() / 1000, main: 'main.tex' })
      return [pdf, log]
    }""", [pdf(text), text])
    return ids[0], ids[1]


def main() -> None:
    with webcheck.Session() as s:
        owner = s.project(FILES, name="Compiler")
        owner_asked: list[str] = []
        owner.on("request", lambda rq: owner_asked.append(rq.url.rsplit("/", 1)[-1]) if "/blob/" in rq.url and rq.method == "GET" else None)
        viewer = s.page()
        asked: list[str] = []  # the blob ids the viewer fetched
        viewer.on("request", lambda rq: asked.append(rq.url.rsplit("/", 1)[-1]) if "/blob/" in rq.url and rq.method == "GET" else None)
        viewer.goto(owner.link)
        viewer.wait_for_selector("dialog[open] button:has-text('Start')", timeout=30000)
        viewer.fill("dialog[open] input[placeholder='Shown to collaborators']", "Viewer")
        viewer.locator("dialog[open] label.radio", has_text="Don’t compile here").last.click()
        viewer.click("dialog[open] button:has-text('Start')")
        viewer.wait_for_function("() => window.__texloom?.r?.synced", timeout=60000)
        viewer.wait_for_function("() => __texloom.r.files.has('main.tex')", timeout=30000)

        def wait(cond, secs: float = 10) -> bool:
            try:
                viewer.wait_for_function(cond, timeout=secs * 1000)
                return True
            except Exception:  # noqa: BLE001
                return False

        def count(bid: str) -> int:
            return asked.count(bid)

        def drawn() -> int:  # PDF pages the viewer has on screen
            return viewer.locator(".pane.output canvas").count()

        def settle() -> None:
            viewer.wait_for_timeout(1500)

        tab = lambda name: viewer.locator("button.tab", has_text=name)

        # showing the PDF
        one, _ = compile_as(owner, "ONE")
        settle()
        s.check("a compile is fetched once, by a browser showing the PDF", count(one) == 1, f"fetched {count(one)} times")
        s.check("and it is on screen", wait("() => document.querySelectorAll('.pane.output canvas').length > 0") and "Compiler" in viewer.locator(".pdfinfo").inner_text())

        # on another tab
        tab("Assistant").click()
        two, two_log = compile_as(owner, "TWO")
        settle()
        s.check("with another tab showing, the compile's PDF is not fetched", count(two) == 0, f"fetched {count(two)} times")
        s.check("but its log is (the editor marks the problems from it)", count(two_log) == 1, f"fetched {count(two_log)} times")
        tab("PDF").click()
        settle()
        s.check("coming back to the PDF fetches the newest, once", count(two) == 1, f"fetched {count(two)} times")
        s.check("and nothing again for the one it has", count(one) == 1)

        # several while away: only the last
        tab("Assistant").click()
        three, _ = compile_as(owner, "THREE")
        four, _ = compile_as(owner, "FOUR")
        five, _ = compile_as(owner, "FIVE")
        settle()
        tab("PDF").click()
        settle()
        s.check("after several compiles, only the last is fetched", [count(three), count(four), count(five)] == [0, 0, 1],
                f"fetched {[count(three), count(four), count(five)]}")

        # the panel closed
        viewer.locator("button.pane-toggle").last.click()
        six, _ = compile_as(owner, "SIX")
        settle()
        s.check("with the PDF panel closed, the PDF is not fetched", count(six) == 0, f"fetched {count(six)} times")
        viewer.locator("button.pane-toggle").last.click()
        settle()
        s.check("opening it fetches the newest", count(six) == 1, f"fetched {count(six)} times")

        # popped out: the window wants each PDF, wherever the main window is
        with viewer.context.expect_page() as popup:
            viewer.locator("button.to-window").click()
        win = popup.value
        win.wait_for_load_state()
        tab("Assistant").click()
        seven, _ = compile_as(owner, "SEVEN")
        settle()
        s.check("a PDF popped out into its own window is fetched for it, even with another tab showing", count(seven) == 1, f"fetched {count(seven)} times")
        try:
            win.wait_for_function("() => document.querySelectorAll('canvas').length > 0", timeout=10000)
            got = True
        except Exception:  # noqa: BLE001
            got = False
        s.check("and the window shows it", got)
        win.close()
        settle()
        eight, _ = compile_as(owner, "EIGHT")
        settle()
        s.check("with the window closed it stops: the next compile is not fetched", count(eight) == 0, f"fetched {count(eight)} times")

        made = [one, two, three, four, five, six, seven, eight]
        s.check("the compiling browser, which has what it made, fetched none of them", not [b for b in owner_asked if b in made], str(owner_asked))
        s.done()


if __name__ == "__main__":
    main()
