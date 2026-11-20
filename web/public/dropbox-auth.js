// Where Dropbox sends this tab back after signing in (web/src/dropbox.ts signIn): swap the code for
// tokens with the PKCE verifier the asking page left in this tab's sessionStorage, keep the account
// where the web app looks for it (localStorage texloom.dropbox, as accounts.ts keeps one), and go
// back to the project. No secret is involved; the code is worth nothing without the verifier.
(function () {
  var q = new URLSearchParams(location.search)
  history.replaceState(null, '', location.pathname) // keep the code out of the history
  var pending = null
  try { pending = JSON.parse(sessionStorage.getItem('texloom.dropboxPending') || 'null'); sessionStorage.removeItem('texloom.dropboxPending') } catch (e) {}
  function say(text) {
    var go = function () { var m = document.getElementById('msg'); if (m) m.textContent = text }
    if (document.readyState === 'loading') window.addEventListener('DOMContentLoaded', go); else go()
  }
  function back(problem) {
    if (problem) try { sessionStorage.setItem('texloom.dropboxProblem', problem) } catch (e) {}
    location.replace(pending && pending.back ? pending.back : '/')
  }
  if (!pending || pending.state !== q.get('state')) { say('This sign-in didn’t start here. Go back to TeXLoom and press Connect Dropbox again.'); return }
  if (!q.get('code')) { back(q.get('error_description') || q.get('error') || 'no code'); return }
  var account
  fetch(pending.api + '/oauth2/token', {
    method: 'POST', cache: 'no-store',
    body: new URLSearchParams({ grant_type: 'authorization_code', code: q.get('code'), code_verifier: pending.verifier, client_id: pending.key, redirect_uri: pending.redirect }),
  }).then(function (r) { return r.json().then(function (j) { if (!r.ok) throw new Error(j.error_description || j.error || ('HTTP ' + r.status)); return j }) })
    .then(function (tok) {
      account = { refresh: tok.refresh_token, access: tok.access_token, expires: Date.now() + tok.expires_in * 1000 - 60000, id: String(tok.account_id || ''), name: '', key: pending.key }
      return fetch(pending.api + '/2/users/get_current_account', { method: 'POST', cache: 'no-store', headers: { Authorization: 'Bearer ' + tok.access_token, 'Content-Type': 'application/json' }, body: 'null' })
    })
    .then(function (r) { return r.ok ? r.json() : {} })
    .then(function (me) {
      account.name = String((me && me.name && me.name.display_name) || '')
      localStorage.setItem('texloom.dropbox', JSON.stringify(account))
      localStorage.setItem('texloom.dropboxAt', String(Date.now()))
      back('')
    })
    .catch(function (e) { back(String((e && e.message) || e)) })
})()
