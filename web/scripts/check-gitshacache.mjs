// Checks the cache of git ids and Dropbox hashes (src/gitshacache.ts, issue 29): it keeps only the files the project has
// now, so it doesn't grow with every version of every image ever synced, and survives bad storage.
import { browser, load, tally } from './lib.mjs'

const [{ shaCache, cacheKey }] = await load('src/gitshacache.ts')

const store = new Map()
let writes = 0, broken = false
browser({ localStorage: {
  getItem: (k) => { if (broken) throw new Error('blocked'); return store.has(k) ? store.get(k) : null },
  setItem: (k, v) => { if (broken) throw new Error('blocked'); writes++; store.set(k, String(v)) },
  removeItem: (k) => { if (broken) throw new Error('blocked'); writes++; store.delete(k) },
} })
const { check, report } = tally()
const stored = (p) => JSON.parse(store.get(cacheKey(p)) ?? '{}')

// a project whose image changes every sync: without trimming, one entry per version
let current = ['logo-v0']
for (let v = 0; v < 500; v++) {
  current = [`logo-v${v}`, 'photo']
  const c = shaCache('p1', current)
  for (const h of current) if (!c.get(h)) c.set(h, `sha-of-${h}`)
}
check('only the files the project has now are kept', Object.keys(stored('p1')).sort().join() === 'logo-v499,photo', Object.keys(stored('p1')).length)
const c = shaCache('p1', ['logo-v499', 'photo'])
check('and their ids are still there', c.get('logo-v499') === 'sha-of-logo-v499' && c.get('photo') === 'sha-of-photo')
writes = 0
shaCache('p1', ['logo-v499', 'photo'])
check('opening it with nothing to drop writes nothing', writes === 0, writes)
c.set('photo', 'sha-of-photo')
check('setting what it already has writes nothing', writes === 0, writes)
shaCache('p1', [])
check('a project with no stored files leaves no key behind', !store.has(cacheKey('p1')), store.get(cacheKey('p1')))
shaCache('p2', ['a']).set('a', 'sha-a')
shaCache('p3', ['b']).set('b', 'sha-b')
shaCache('p2', [])
check('each project has its own', store.has(cacheKey('p3')) && !store.has(cacheKey('p2')))
store.set(cacheKey('p4'), 'not json')
check('unreadable contents start afresh', shaCache('p4', ['x']).get('x') === undefined)
store.set(cacheKey('p5'), JSON.stringify(['x']))
check('contents of the wrong shape start afresh', shaCache('p5', ['0']).get('0') === undefined)
store.set(cacheKey('p6'), JSON.stringify({ x: 5, y: 'sha-y' }))
check('an entry that isn’t an id is dropped', shaCache('p6', ['x', 'y']).get('x') === undefined && stored('p6').y === 'sha-y')
shaCache('p8', ['a']).set('a', 'sha-a')
shaCache('p8', ['a'], 'texloom.dropboxHash.p8').set('a', 'dropbox-a')
shaCache('p8', [], 'texloom.dropboxHash.p8')
check('Dropbox’s hashes are kept apart, and trimmed the same way', stored('p8').a === 'sha-a' && !store.has('texloom.dropboxHash.p8'))
broken = true
const b = shaCache('p7', ['z'])
b.set('z', 'sha-z')
check('blocked storage: it still works for this visit', b.get('z') === 'sha-z')

report()
