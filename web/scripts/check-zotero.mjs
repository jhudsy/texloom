// Checks that refreshing a Zotero link (src/zotero.ts) reads only what this browser's user chose: a
// link in the project's shared settings that a collaborator made or changed, pointing at your own
// library (Zotero user ids are public), isn't fetched with your key unless you say so (the security
// review of 2026-11-24). Zotero itself is a stand-in here: nothing goes online.
import * as Y from 'yjs'
import { browser, load, tally } from './lib.mjs'

const store = new Map()
browser({ localStorage: {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => { store.set(k, String(v)) },
  removeItem: (k) => { store.delete(k) },
} })
const asked = []
globalThis.fetch = async (url) => {
  asked.push(String(url))
  const u = new URL(url)
  if (u.pathname.endsWith('/groups')) return new Response('[]', { headers: { 'Total-Results': '0' } })
  return new Response('@article{secret, title={My private entry}}', { headers: { 'Total-Results': '1' } })
}
const [{ refresh, setLink, approve, zoteroAccount }] = await load('src/zotero.ts')
const { check, report } = tally()

const doc = new Y.Doc()
const r = { doc, files: doc.getMap('files'), meta: doc.getMap('meta'), keys: { project: 'p1' } }
zoteroAccount.set({ key: 'k'.repeat(24), userID: 42, username: 'me' })
const mine = { lib: 'users/42', libName: 'My Library', coll: null, collName: 'The whole library', format: 'bibtex', by: 'Mallory', at: 1 }

// a collaborator's link to your own library: not fetched without asking
setLink(r, 'refs.bib', mine)
let error = ''
try { await refresh(r, 'refs.bib', 'me') } catch (e) { error = String(e.message) }
check('a link this browser didn’t make isn’t refreshed', error && !r.files.get('refs.bib'), error)
check('and nothing is fetched from the library', !asked.some((a) => a.includes('/items')), asked)

let question = null
await refresh(r, 'refs.bib', 'me', (file, link) => { question = [file, link.lib]; return false }).catch(() => {})
check('it asks first, naming the file and the library', question?.[0] === 'refs.bib' && question?.[1] === 'users/42', question)
check('declined, nothing is fetched', !r.files.get('refs.bib'))

await refresh(r, 'refs.bib', 'me', () => true)
check('agreed, it is fetched', String(r.files.get('refs.bib') ?? '').includes('My private entry'))
asked.length = 0
await refresh(r, 'refs.bib', 'me')
check('and the next refresh needs no question', asked.some((a) => a.includes('/items')))

// the link changed by someone else afterwards (another collection): asks again
setLink(r, 'refs.bib', { ...mine, coll: 'ABCD1234' })
error = ''
try { await refresh(r, 'refs.bib', 'me') } catch (e) { error = String(e.message) }
check('a link changed by someone else asks again', !!error, error)

// one made in this browser (the link dialog approves it) is refreshed without asking
setLink(r, 'other.bib', { ...mine, coll: 'WXYZ5678' })
approve(r, 'other.bib', { ...mine, coll: 'WXYZ5678' })
await refresh(r, 'other.bib', 'me')
check('a link made here is refreshed without asking', String(r.files.get('other.bib') ?? '').includes('@article'))

report()
