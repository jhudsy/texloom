// Runs latexdiff (Perl) in the Perl that the in-browser TeX engine ships for Biber, as
// texcount-worker.js runs TeXCount. A plain script in public/ because the engine's loader is a
// classic script, which module workers can't import.
//
// In: { base, script, older: [[path, text]], newer: [[path, text]], main, args }
// Out: { status, tex, messages }: the marked-up document (stdout) and what latexdiff said (stderr).
self.onmessage = async ({ data: { base, script, older, newer, main, args } }) => {
  const out = [], said = []
  try {
    importScripts(`${base}/biber.js`)
    const Module = await self.biber({
      noInitialRun: true,
      locateFile: (p) => (p.endsWith('.wasm') ? `${base}/biber.wasm` : p.endsWith('.data') ? `${base}/biber.data` : p),
      print: (text) => out.push(text),
      printErr: (text) => said.push(text),
    })
    const FS = Module.FS, home = '/home/web_user'
    const write = (root, path, contents) => {
      let dir = ''
      for (const part of `${root}/${path}`.split('/').slice(1, -1)) { dir += `/${part}`; if (!FS.analyzePath(dir).exists) FS.mkdir(dir) }
      FS.writeFile(`${root}/${path}`, contents)
    }
    FS.writeFile(`${home}/latexdiff.pl`, script)
    for (const [path, text] of older) write(`${home}/old`, path, text)
    for (const [path, text] of newer) write(`${home}/new`, path, text)
    FS.chdir(home)
    let status = 0
    try { status = Module.callMain([`${home}/latexdiff.pl`, ...args, `old/${main}`, `new/${main}`]) } catch (e) { status = typeof e.status === 'number' ? e.status : 1; if (typeof e.status !== 'number') said.push(String(e)) }
    self.postMessage({ status, tex: out.join('\n') + '\n', messages: said.join('\n') })
  } catch (e) {
    self.postMessage({ status: 1, tex: '', messages: `${said.join('\n')}\n${e}` })
  }
}
