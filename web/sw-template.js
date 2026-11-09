// TeXLoom service worker (generated at build time from web/sw-template.js).
// Makes the app itself available offline: projects already live in IndexedDB, and
// this caches the code that opens them. Sync traffic (/ws/, /blob/, /api/) is never cached.
const VERSION = __VERSION__
const SHELL = __SHELL__ // every built asset, so parts not yet opened also work offline
const CACHE = `texloom-${VERSION}`
const TEX = 'texloom-tex' // in-browser TeX engine scripts and WebAssembly (kept until the engine changes)
const ENGINE = __ENGINE__ // the engine these should be from
const ENGINE_MARK = '/__engine-version' // in the cache: the engine what's there is from
const FIRST_MARKED_ENGINE = '1.4.1' // caches from before the mark existed
const TEXLIVE = 'texloom-texlive' // TeX Live files fetched on demand (URLs name the release; kept across versions)

// Each file is cached on its own: one that fails (a stale list, a dropped connection)
// mustn't stop the update, or the previous version would stay in charge indefinitely.
self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE)
    await Promise.allSettled(SHELL.map((p) => cache.add(p)))
    await self.skipWaiting()
  })())
})

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) if (key !== CACHE && key !== TEX && key !== TEXLIVE) await caches.delete(key)
    // The engine's files keep their names between versions: the old WebAssembly mustn't meet the new scripts.
    const mark = await (await caches.open(TEX)).match(ENGINE_MARK)
    if ((mark ? await mark.text() : FIRST_MARKED_ENGINE) !== ENGINE) await caches.delete(TEX)
    await (await caches.open(TEX)).put(ENGINE_MARK, new Response(ENGINE))
    await self.clients.claim()
  })())
})

self.addEventListener('fetch', (event) => {
  const req = event.request
  if (req.method !== 'GET') return
  const url = new URL(req.url)
  if (url.origin !== location.origin) return
  const p = url.pathname
  if (p.startsWith('/ws/') || p.startsWith('/blob/') || p.startsWith('/api/') || p === '/admin') return

  // The page: network first (so updates arrive), the cached copy when offline.
  if (req.mode === 'navigate') {
    event.respondWith((async () => {
      try {
        const ctrl = new AbortController()
        const timer = setTimeout(() => ctrl.abort(), 4000)
        const res = await fetch(req, { signal: ctrl.signal })
        clearTimeout(timer)
        // only the app's own page (not /admin or anything else the server answers)
        if (res.ok && p === '/' && (res.headers.get('content-type') || '').startsWith('text/html')) (await caches.open(CACHE)).put('/index.html', res.clone())
        return res
      } catch {
        return (await caches.match('/index.html')) || (await caches.match('/')) || Response.error()
      }
    })())
    return
  }

  // In-browser TeX: engine scripts/WebAssembly cached on first use. Its large package
  // data (*.data) is cached by the engine itself in IndexedDB, so it's left alone here.
  // The small scripts are checked with the server first (deploys can patch them), the
  // large WebAssembly is taken from the cache.
  if (p.startsWith('/core/') && !p.endsWith('.data')) {
    event.respondWith(p.endsWith('.js') ? networkFirst(req, TEX) : cacheFirst(req, TEX))
    return
  }

  // TeX Live files on demand (/texlive/<year>/<format>/<name>): never change, so cache first;
  // also lets compiles that used them work offline.
  if (p.startsWith('/texlive/')) {
    event.respondWith(cacheFirst(req, TEXLIVE))
    return
  }

  // Hashed build output and static files: cache first.
  event.respondWith(cacheFirst(req, CACHE))
})

/** The server's current copy (revalidated, so cheap when unchanged); the cached one offline. */
async function networkFirst(req, name) {
  try {
    const res = await fetch(req, { cache: 'no-cache' })
    if (res.ok && res.type === 'basic') (await caches.open(name)).put(req, res.clone())
    return res
  } catch {
    return (await caches.match(req)) || Response.error()
  }
}

async function cacheFirst(req, name) {
  const hit = await caches.match(req)
  if (hit) return hit
  const res = await fetch(req)
  if (res.ok && res.type === 'basic') (await caches.open(name)).put(req, res.clone())
  return res
}
