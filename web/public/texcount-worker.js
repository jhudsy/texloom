// Runs TeXCount (Perl) in the Perl that the in-browser TeX engine ships for Biber. A plain script
// in public/ because the engine's loader is a classic script, which module workers can't import.
self.onmessage = async ({ data: { base, script, files, main, args } }) => {
  const output = []
  try {
    importScripts(`${base}/biber.js`)
    const Module = await self.biber({
      noInitialRun: true,
      locateFile: (p) => (p.endsWith('.wasm') ? `${base}/biber.wasm` : p.endsWith('.data') ? `${base}/biber.data` : p),
      print: (text) => output.push(text),
      printErr: (text) => output.push(text),
    })
    const FS = Module.FS, root = '/home/web_user/project'
    const write = (path, contents) => {
      let dir = ''
      for (const part of `${root}/${path}`.split('/').slice(1, -1)) { dir += `/${part}`; if (!FS.analyzePath(dir).exists) FS.mkdir(dir) }
      FS.writeFile(`${root}/${path}`, contents)
    }
    FS.writeFile('/home/web_user/texcount.pl', script)
    for (const [path, text] of files) write(path, text)
    FS.chdir(root)
    let status = 0
    try { status = Module.callMain(['/home/web_user/texcount.pl', ...args, main]) } catch (e) { status = typeof e.status === 'number' ? e.status : 1 }
    self.postMessage({ status, output: output.join('\n') })
  } catch (e) {
    self.postMessage({ status: 1, output: `${output.join('\n')}\n${e}` })
  }
}
