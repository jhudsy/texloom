// TeXLoom service worker (generated at build time from web/sw-template.js).
// Makes the app itself available offline: projects already live in IndexedDB, and
// this caches the code that opens them. Sync traffic (/ws/, /blob/, /api/) is never cached.
const VERSION = __VERSION__
const SHELL = __SHELL__ // every built asset, so parts not yet opened also work offline
const CACHE = `texloom-${VERSION}`
const TEX = 'texloom-tex' // in-browser TeX engine scripts and WebAssembly (kept across versions)

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()))
})

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) if (key !== CACHE && key !== TEX) await caches.delete(key)
    await self.clients.claim()
  })())
})

self.addEventListener('fetch', (event) => {
  const req = event.request
  if (req.method !== 'GET') return
  const url = new URL(req.url)
  if (url.origin !== location.origin) return
  const p = url.pathname
  if (p.startsWith('/ws/') || p.startsWith('/blob/') || p.startsWith('/api/')) return

  // The page: network first (so updates arrive), the cached copy when offline.
  if (req.mode === 'navigate') {
    event.respondWith((async () => {
      try {
        const ctrl = new AbortController()
        const timer = setTimeout(() => ctrl.abort(), 4000)
        const res = await fetch(req, { signal: ctrl.signal })
        clearTimeout(timer)
        if (res.ok) (await caches.open(CACHE)).put('/index.html', res.clone())
        return res
      } catch {
        return (await caches.match('/index.html')) || (await caches.match('/')) || Response.error()
      }
    })())
    return
  }

  // In-browser TeX: engine scripts/WebAssembly cached on first use. Its large package
  // data (*.data) is cached by the engine itself in IndexedDB, so it's left alone here.
  if (p.startsWith('/core/') && !p.endsWith('.data')) {
    event.respondWith(cacheFirst(req, TEX))
    return
  }

  // Hashed build output and static files: cache first.
  event.respondWith(cacheFirst(req, CACHE))
})

async function cacheFirst(req, name) {
  const hit = await caches.match(req)
  if (hit) return hit
  const res = await fetch(req)
  if (res.ok && res.type === 'basic') (await caches.open(name)).put(req, res.clone())
  return res
}
