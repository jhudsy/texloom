// TeXLoom comments in VS Code (issue #36). Starts `texloom lsp` (the language server in
// texloom/lsp.py) for the folder you have open, and shows its comments as native comment threads
// beside the text, with reply and resolve, and/or as Problems-panel entries.
// All it knows of a project is what the server answers to `texloom/comments`.
const vscode = require('vscode')
const cp = require('child_process')
const fs = require('fs')
const { LanguageClient } = require('vscode-languageclient/node')

const SELECTOR = [{ scheme: 'file', language: 'latex' }, { scheme: 'file', language: 'bibtex' }, { scheme: 'file', language: 'tex' }]
const MAC_APP = '/Applications/TeXLoom.app/Contents/MacOS/TeXLoom'

/** How to run TeXLoom: the setting, else `texloom` on the PATH, else the Mac app. */
function command() {
  const set = vscode.workspace.getConfiguration('texloom').get('command')
  if (set && set.trim()) return set.trim()
  const probe = cp.spawnSync('texloom', ['--help'], { stdio: 'ignore', shell: process.platform === 'win32' })
  if (!probe.error && probe.status === 0) return 'texloom'
  if (process.platform === 'darwin' && fs.existsSync(MAC_APP)) return MAC_APP
  return null
}

let client, controller
const threads = new Map() // comment id -> CommentThread (per file: key `${uri}#${id}`)
const ids = new WeakMap() // CommentThread -> comment id
let timer, name = ''

function style() { return vscode.workspace.getConfiguration('texloom').get('comments') }

function body(c) {
  const who = (n, v) => (v ? `${n} ✓` : n)
  if (c.kind === 'suggestion') {
    const md = new vscode.MarkdownString(c.quote ? `Suggested change: replace\n\n\`\`\`\n${c.quote}\n\`\`\`\nwith\n\n\`\`\`\n${c.text}\n\`\`\`` : `Suggested insertion:\n\n\`\`\`\n${c.text}\n\`\`\``)
    md.appendMarkdown('\n\n_Accept or reject it in the Review tab, where you can see the change._')
    return { body: md, mode: vscode.CommentMode.Preview, author: { name: who(c.by, c.verified) } }
  }
  return { body: new vscode.MarkdownString(c.text), mode: vscode.CommentMode.Preview, author: { name: who(c.by, c.verified) } }
}

function comments(c) {
  const first = body(c)
  return [first, ...c.replies.map((rp) => ({ body: new vscode.MarkdownString(rp.text), mode: vscode.CommentMode.Preview, author: { name: rp.verified ? `${rp.by} ✓` : rp.by } }))]
}

async function show(editor) {
  const doc = editor.document
  if (!client || !SELECTOR.some((s) => s.language === doc.languageId) || doc.uri.scheme !== 'file') return
  let res
  try {
    res = await client.sendRequest('texloom/comments', { textDocument: { uri: doc.uri.toString() } })
  } catch { return }
  name = res.name
  const want = style() !== 'problems'
  const prefix = `${doc.uri.toString()}#`
  const keep = new Set()
  for (const c of want ? res.comments : []) {
    const key = prefix + c.id
    keep.add(key)
    const r = c.range
    const range = new vscode.Range(r.start.line, r.start.character, r.end.line, r.end.character)
    let th = threads.get(key)
    if (!th) {
      th = controller.createCommentThread(doc.uri, range, [])
      ids.set(th, c.id)
      threads.set(key, th)
    }
    th.range = range
    th.comments = comments(c)
    th.label = c.mentionsMe ? 'Mentions you' : c.kind === 'suggestion' ? 'Suggested change' : 'Comment'
    th.contextValue = res.canWrite && c.kind === 'comment' ? 'texloom-open' : 'texloom-readonly'
    th.canReply = !!res.canWrite
    th.state = vscode.CommentThreadState.Unresolved
    th.collapsibleState = c.mentionsMe ? vscode.CommentThreadCollapsibleState.Expanded : vscode.CommentThreadCollapsibleState.Collapsed
  }
  for (const [key, th] of threads) {
    if (key.startsWith(prefix) && !keep.has(key)) { th.dispose(); threads.delete(key) }
  }
}

function refresh(delay = 300) {
  clearTimeout(timer)
  timer = setTimeout(() => vscode.window.visibleTextEditors.forEach(show), delay)
}

async function run(cmd, args) {
  try {
    await client.sendRequest('workspace/executeCommand', { command: cmd, arguments: args })
  } catch (e) {
    vscode.window.showErrorMessage(`TeXLoom: ${e.message}`)
  }
  refresh(0)
}

function activate(context) {
  const api = { threads } // (for the extension's own test)
  const cmd = command()
  if (!cmd) {
    vscode.window.showWarningMessage('TeXLoom comments needs the TeXLoom app or the texloom command-line helper (texloom.org/download). Install it, or set “texloom.command”.')
    return api
  }
  controller = vscode.comments.createCommentController('texloom', 'TeXLoom comments')
  controller.options = { placeHolder: 'Reply…', prompt: 'Reply to this comment' }
  context.subscriptions.push(controller)

  client = new LanguageClient('texloom', 'TeXLoom', { command: cmd, args: ['lsp'] }, {
    documentSelector: SELECTOR,
    middleware: {
      // Problems-panel entries only when asked for: threads are the default view
      handleDiagnostics: (uri, diagnostics, next) => next(uri, style() === 'threads' ? [] : diagnostics),
    },
  })
  context.subscriptions.push({ dispose: () => client && client.stop() })
  client.start().then(() => {
    client.onNotification('texloom/changed', () => refresh(0))
    refresh(0)
  }).catch((e) => vscode.window.showErrorMessage(`TeXLoom: ${e.message}`))

  context.subscriptions.push(
    vscode.commands.registerCommand('texloom.replyThread', (reply) => {
      const id = ids.get(reply.thread)
      const text = (reply.text || '').trim()
      if (id && text) run('texloom.reply', [id, text])
    }),
    vscode.commands.registerCommand('texloom.resolveThread', (thread) => {
      const id = ids.get(thread)
      if (id) run('texloom.resolve', [id])
    }),
    vscode.commands.registerCommand('texloom.showReview', () => run('texloom.openReview', [])), // (the server's own command is texloom.openReview)
    vscode.commands.registerCommand('texloom.refresh', () => refresh(0)),
    vscode.window.onDidChangeVisibleTextEditors(() => refresh()),
    vscode.workspace.onDidChangeTextDocument((e) => { if (e.document.uri.scheme === 'file') refresh(500) }),
    vscode.workspace.onDidChangeConfiguration((e) => { if (e.affectsConfiguration('texloom.comments')) refresh(0) }),
  )
  return api
}

function deactivate() { return client && client.stop() }

module.exports = { activate, deactivate }
