// A small MCP client for the browser (Streamable HTTP transport): initialise, list tools, call one.
// The requests go from this browser straight to the server the person added, never through the
// texloom server, so only servers on the web that allow this page (CORS) can be reached.
// Imports nothing that needs a page: scripts/check-mcpclient.mjs runs it in node against a stand-in server.
import { quiet } from '../quiet'

export const PROTOCOL = '2025-06-18'
const CLIENT = { name: 'texloom-assistant', version: '1' }
const MAX_TOOLS = 128 // per server: more would crowd out the conversation
const MAX_RESULT = 100_000 // characters of one result handed to the model

export interface McpTool { name: string; description: string; inputSchema: Record<string, unknown> }
export interface McpServerInfo { name: string; version?: string; instructions?: string }

export class McpError extends Error {
  constructor(message: string, readonly kind: 'network' | 'http' | 'protocol' | 'tool' = 'protocol') { super(message) }
}

/** One connection to one server: `connect()` once, then `listTools()` and `callTool()`. */
export class McpClient {
  private sessionId: string | null = null
  private version = PROTOCOL
  private next = 1
  info: McpServerInfo | null = null

  constructor(readonly url: string, private token = '', private fetcher: typeof fetch = (...a) => fetch(...a)) {}

  private headers(): Record<string, string> {
    const h: Record<string, string> = { 'Content-Type': 'application/json', Accept: 'application/json, text/event-stream' }
    if (this.token) h.Authorization = `Bearer ${this.token}`
    if (this.sessionId) h['Mcp-Session-Id'] = this.sessionId
    if (this.info) h['MCP-Protocol-Version'] = this.version
    return h
  }

  private async post(body: unknown, signal?: AbortSignal): Promise<Response> {
    let r: Response
    try {
      r = await this.fetcher(this.url, { method: 'POST', headers: this.headers(), body: JSON.stringify(body), signal })
    } catch (e) {
      if (signal?.aborted) throw e
      throw new McpError(String((e as Error)?.message ?? e), 'network')
    }
    if (!r.ok && r.status !== 202) {
      const text = await r.text().catch(() => '')
      throw new McpError(`HTTP ${r.status}${text ? `: ${text.slice(0, 200)}` : ''}`, 'http')
    }
    return r
  }

  /** A request and its response, which comes as JSON or as an event stream that contains it. */
  async request(method: string, params: Record<string, unknown> = {}, signal?: AbortSignal): Promise<any> {
    const id = this.next++
    const r = await this.post({ jsonrpc: '2.0', id, method, params }, signal)
    const sid = r.headers.get('Mcp-Session-Id')
    if (sid) this.sessionId = sid
    const type = r.headers.get('Content-Type') ?? ''
    const reply = type.includes('text/event-stream') ? await fromStream(r, id) : await r.json().catch(() => null)
    if (!reply || typeof reply !== 'object') throw new McpError(`No reply to ${method}`)
    if (reply.error) throw new McpError(String(reply.error.message ?? JSON.stringify(reply.error)).slice(0, 500))
    return reply.result
  }

  private async notify(method: string): Promise<void> {
    const r = await this.post({ jsonrpc: '2.0', method })
    await r.body?.cancel().catch((e) => quiet('a notification’s empty reply wasn’t read to the end', e))
  }

  async connect(signal?: AbortSignal): Promise<McpServerInfo> {
    const res = await this.request('initialize', { protocolVersion: PROTOCOL, capabilities: {}, clientInfo: CLIENT }, signal)
    if (!res || typeof res !== 'object') throw new McpError('The server’s reply to initialize is empty')
    this.version = typeof res.protocolVersion === 'string' ? res.protocolVersion : PROTOCOL
    this.info = { name: String(res.serverInfo?.name ?? ''), version: res.serverInfo?.version, instructions: typeof res.instructions === 'string' ? res.instructions : undefined }
    await this.notify('notifications/initialized')
    return this.info
  }

  async listTools(signal?: AbortSignal): Promise<McpTool[]> {
    const tools: McpTool[] = []
    let cursor: string | undefined
    for (let page = 0; page < 20 && tools.length < MAX_TOOLS; page++) {
      const res = await this.request('tools/list', cursor ? { cursor } : {}, signal)
      for (const t of Array.isArray(res?.tools) ? res.tools : []) {
        if (typeof t?.name !== 'string' || !t.name) continue
        const schema = t.inputSchema && typeof t.inputSchema === 'object' ? t.inputSchema : { type: 'object', properties: {} }
        tools.push({ name: t.name, description: String(t.description ?? '').slice(0, 4000), inputSchema: { ...schema, type: 'object' } })
      }
      cursor = typeof res?.nextCursor === 'string' && res.nextCursor ? res.nextCursor : undefined
      if (!cursor) break
    }
    return tools.slice(0, MAX_TOOLS)
  }

  /** A tool's result as text for the model; `isError` when the tool said it failed. */
  async callTool(name: string, args: Record<string, unknown>, signal?: AbortSignal): Promise<{ text: string; isError: boolean }> {
    const res = await this.request('tools/call', { name, arguments: args }, signal)
    return { text: resultText(res), isError: !!res?.isError }
  }

  /** Ends the session on the server, if it keeps one (best effort). */
  async close(): Promise<void> {
    if (!this.sessionId) return
    await this.fetcher(this.url, { method: 'DELETE', headers: this.headers() }).catch((e) => quiet('ending an MCP session is best effort: the server drops it in time', e))
    this.sessionId = null
  }
}

/** The JSON-RPC response with this id, from an event stream (other messages before it are skipped). */
async function fromStream(r: Response, id: number): Promise<any> {
  const reader = r.body?.getReader()
  if (!reader) return null
  const dec = new TextDecoder()
  let buf = ''
  let data: string[] = []
  try {
    for (;;) {
      const { value, done } = await reader.read()
      if (value) buf += dec.decode(value, { stream: true })
      let nl: number
      while ((nl = buf.search(/\r?\n/)) >= 0) {
        const line = buf.slice(0, nl)
        buf = buf.slice(nl + (buf[nl] === '\r' ? 2 : 1))
        if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''))
        else if (line === '' && data.length) {
          const msg = parse(data.join('\n'))
          data = []
          if (msg && msg.id === id && ('result' in msg || 'error' in msg)) return msg
        }
      }
      if (done) {
        const msg = data.length ? parse(data.join('\n')) : null
        return msg && msg.id === id ? msg : null
      }
    }
  } finally {
    reader.cancel().catch((e) => quiet('the rest of an event stream after its reply isn’t needed', e))
  }
}

function parse(s: string): any {
  try { return JSON.parse(s) } catch { return null }
}

/** What a tool returned, as text: its text parts, with other kinds of content named. */
export function resultText(res: any): string {
  const parts: string[] = []
  for (const c of Array.isArray(res?.content) ? res.content : []) {
    if (c?.type === 'text') parts.push(String(c.text ?? ''))
    else if (c?.type === 'resource' && typeof c.resource?.text === 'string') parts.push(c.resource.text)
    else if (c?.type === 'resource_link') parts.push(`[${c.name ?? 'resource'}: ${c.uri ?? ''}]`)
    else if (c?.type) parts.push(`[${c.type} content, not shown]`)
  }
  if (!parts.length && res?.structuredContent !== undefined) parts.push(JSON.stringify(res.structuredContent))
  const text = parts.join('\n\n')
  return text.length > MAX_RESULT ? `${text.slice(0, MAX_RESULT)}\n[… cut to ${MAX_RESULT} characters]` : text
}

/** A prefix for a server's tools, from its name: letters, digits and _ only, as providers require. */
export function prefixFor(name: string, taken: Set<string>): string {
  let p = name.normalize('NFKD').replace(/[^\w]+/g, '_').replace(/^_+|_+$/g, '').toLowerCase().slice(0, 20) || 'server'
  if (/^\d/.test(p)) p = `s_${p}`
  let q = p
  for (let i = 2; taken.has(q); i++) q = `${p}_${i}`
  return q
}

/** A tool's name as the model sees it: `prefix__name`, within the 64 characters providers allow. */
export function qualified(prefix: string, tool: string): string {
  return `${prefix}__${tool.replace(/[^\w-]/g, '_')}`.slice(0, 64)
}
