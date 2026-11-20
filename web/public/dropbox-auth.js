// Where Dropbox sends the sign-in pop-up back (web/src/dropbox.ts signIn): hand the answer to the
// TeXLoom page that asked, through a BroadcastChannel (by now the pop-up has no opener), then close.
// The code is worth nothing without the asking page's PKCE verifier, which never leaves that page.
(function () {
  var q = new URLSearchParams(location.search)
  var answer = { state: q.get('state'), code: q.get('code'), error: q.get('error') }
  history.replaceState(null, '', location.pathname) // keep the code out of the history
  try { var ch = new BroadcastChannel('texloom-dropbox-auth'); ch.postMessage(answer); ch.close() } catch (e) {}
  window.addEventListener('DOMContentLoaded', function () {
    var msg = document.getElementById('msg')
    if (msg) msg.textContent = answer.code ? 'Dropbox is connected. You can close this window.' : 'Dropbox wasn’t connected. You can close this window and try again.'
  })
  setTimeout(function () { window.close() }, 800)
})()
