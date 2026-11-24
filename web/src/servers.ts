import { quiet } from './quiet'
import { getString } from './store'
// Projects can live on other TeXLoom servers than this one. This web app (and your project list,
// settings and creation code) belong to the server it's loaded from, the "home" server; a project
// from elsewhere syncs with its own server, straight from this page. Only encrypted data goes
// there, as here: keys stay in the page. New projects are made on the home server.
//
// A link carries its server as the address it starts with; inside this app, a project from another
// server is opened with "#s=<server>&p=…&k=…", and the list keeps the server with the project.
// No DOM here, so scripts/check-props.mjs can test it.

export const home = (): string => location.origin

/** Whether a host name (URL.hostname) is this computer: localhost, 127.x.x.x or [::1]. */
export const isLoopback = (host: string): boolean => /^(localhost|127\.\d+\.\d+\.\d+|\[::1\])$/.test(host)

/** A setting for testing on this computer (a stand-in for GitHub's or Dropbox's API, test hooks),
 *  kept in localStorage under `key`: only when this app is served from this computer, else null. */
export const devOverride = (key: string): string | null => (isLoopback(location.hostname) ? getString(key) : null)

/** What a server says about itself (GET /api/info, server/endpoints.py). */
export interface ServerInfo {
  creation: 'open' | 'code'
  projectMaxBytes: number
  projectLists?: boolean
  private?: boolean
  codesUrl?: string
  dropboxAppKey?: string
  texlive?: { endpoint?: unknown } | null
}
const infos = new Map<string, Promise<ServerInfo | null>>()
/** What a server says about itself (this app's own by default), or null if it can't be had (asked
 *  again next time: an answer is kept for the page's life). */
export function serverInfo(server: string = home()): Promise<ServerInfo | null> {
  let got = infos.get(server)
  if (!got) {
    got = fetch(`${server}/api/info`, { cache: 'no-store' }).then((r) => (r.ok ? r.json() : null))
      .catch((e) => { quiet('the server didn’t say what it offers (offline?)', e); return null })
    infos.set(server, got)
    void got.then((v) => { if (!v) infos.delete(server) })
  }
  return got
}

/** A server address as an origin ("https://host[:port]"), or null if it can't be one: https only,
 *  except http on this computer (local servers, for testing) or when this app itself is on http. */
export function normalServer(s: string | null | undefined): string | null {
  if (!s) return null
  let u: URL
  try { u = new URL(s) } catch { return null }
  if (u.username || u.password) return null
  if (u.protocol !== 'https:' && !(u.protocol === 'http:' && (isLoopback(u.hostname) || location.protocol === 'http:'))) return null
  return u.origin
}

/** The server to sync a project with: `s` if it's another server's, else the home one. */
export const serverOr = (s: string | null | undefined): string => normalServer(s) ?? home()

/** How a project's server is kept: undefined for the home server, so lists and links stay short. */
export const awayServer = (s: string | null | undefined): string | undefined => {
  const n = normalServer(s)
  return n && n !== home() ? n : undefined
}

/** A server's name for showing: its host, without "www.". */
export const serverName = (s: string): string => { try { return new URL(s).host.replace(/^www\./, '') } catch { return s } }

/** The fragment that opens a project in this app: its keys, and its server if it's another one. */
export const fragmentFor = (fragment: string, server?: string): string => {
  const away = awayServer(server)
  return away ? `s=${encodeURIComponent(away)}&${fragment.replace(/^#/, '')}` : fragment.replace(/^#/, '')
}

/** A pasted link, as the fragment to open it here: null if it isn't a project link. */
export function fragmentOfLink(link: string): string | null {
  const i = link.indexOf('#')
  if (i < 0) return null
  const frag = link.slice(i + 1)
  if (!new URLSearchParams(frag).get('p')) return null
  let origin: string | null = null
  try { origin = new URL(link.slice(0, i)).origin } catch (e) { quiet('not a web address: the link’s own server is used', e) }
  return fragmentFor(frag, origin ?? undefined)
}
