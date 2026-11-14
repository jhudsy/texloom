// Provider adapters. Each session keeps the conversation in its provider's native
// format (append-only, so Claude's thinking blocks stay valid) and runs one model
// turn at a time; the agent loop in agent.ts executes the tools in between.
import Anthropic from '@anthropic-ai/sdk'
import type { ProviderConfig } from './providers'
import { jsonSchema, type ToolDef } from './tools'
import { t } from '../i18n'

export interface ToolCall { id: string; name: string; input: unknown; rawInput?: string }
export type Stop = 'end' | 'tool_use' | 'max_tokens' | 'refusal'
export interface TurnResult { stop: Stop; toolCalls: ToolCall[]; note?: string }
export interface TurnEvents {
  text: (delta: string) => void
  thinking: (delta: string) => void
  toolStart: (name: string) => void
}
export interface LlmSession {
  addUser(text: string): void
  addToolResults(results: { id: string; content: string; isError: boolean }[]): void
  turn(ev: TurnEvents, signal: AbortSignal): Promise<TurnResult>
  /** The conversation so far, to hand back to createSession later. */
  save(): unknown

  // ---- compaction: keeping a long conversation within the model's context ----
  /** How many entries the history has. */
  readonly length: number
  /** About how many tokens the next request sends: the provider's count for the last one plus an
   *  estimate of what has been added since, or an estimate of all of it when the provider didn't say. */
  used(): number
  /** About how many tokens the instructions and tool descriptions take on their own. */
  floor(): number
  /** Where each of the person's own messages starts in the history, oldest first. */
  turnStarts(): number[]
  /** History entries from…to as plain text for a summary, each tool result cut to `maxResult` characters. */
  transcript(from: number, to: number, maxResult: number): string
  /** Replace the history before `keepFrom` (a turn start, or the end) with a summary of it. */
  replaceBefore(keepFrom: number, summary: string): void
  /** Cut the tool results from `from` on to `max` characters, all but the newest round. True if any were cut. */
  shrinkResults(from: number, max: number): boolean
  /** One reply to one prompt, without tools or history (for the summary). */
  ask(system: string, prompt: string, signal: AbortSignal): Promise<string>
  /** Drop Claude's thinking blocks, which are valid only with the instructions they were made under. */
  forgetThinking(): void
  /** The instructions this session sends. */
  readonly system: string
  /** The model's context window in tokens, if the provider can tell (now: it may not know until the model is loaded). */
  contextWindow(): Promise<number | null>
}

// LaTeX and code take more tokens per character than prose: estimate high rather than low.
const CHARS_PER_TOKEN = 3
export const tokensIn = (chars: number) => Math.ceil(chars / CHARS_PER_TOKEN)

/** `s` cut to about `max` characters, keeping its start and end. */
export function cut(s: string, max: number): string {
  if (s.length <= max) return s
  const head = Math.floor(max * 0.7)
  return `${s.slice(0, head)}\n[… ${s.length - max} characters left out …]\n${s.slice(s.length - (max - head))}`
}

const summaryNote = (summary: string) => '<summary>\nThe conversation so far, summarised to make room; it stands in for the earlier messages. ' +
  'Files may have changed since: read them again before relying on details from it.\n' + summary + '\n</summary>\n\n'

const isLocal = (url: string) => /^https?:\/\/(localhost|127\.|\[::1\]|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/i.test(url)
const OLLAMA_DEFAULT_CONTEXT = 4096 // what Ollama loads a model with unless told otherwise (OLLAMA_CONTEXT_LENGTH, num_ctx)
async function getJson(url: string, init?: RequestInit): Promise<any> {
  try {
    const r = await fetch(url, init)
    return r.ok ? await r.json() : null
  } catch { return null }
}

export function createSession(cfg: ProviderConfig, tools: ToolDef[], system: string, saved?: unknown): LlmSession {
  const history = Array.isArray(saved) ? saved : []
  return cfg.kind === 'anthropic' ? new AnthropicSession(cfg, tools, system, history) : new OpenAISession(cfg, tools, system, history)
}

/** Human-readable explanation for network/API failures. */
export function explainError(e: unknown, cfg: ProviderConfig): string {
  if (e instanceof Anthropic.AuthenticationError) return t('Anthropic rejected the API key. Check it in assistant settings.')
  if (e instanceof Anthropic.RateLimitError) return t('Rate limited by Anthropic. Wait a moment and try again.')
  if (e instanceof Anthropic.APIConnectionError || (e instanceof TypeError && /fetch/i.test(e.message)))
    return t('Couldn’t reach {{url}}. Is it running, and does it allow requests from this page (CORS)?', { url: cfg.baseUrl }) +
      (cfg.baseUrl.includes(':1234') ? ' ' + t('For LM Studio: `lms server start --cors`.') : cfg.baseUrl.includes(':11434') ? ' ' + t('For Ollama: set OLLAMA_ORIGINS.') : '')
  if (e instanceof Anthropic.APIError) return t('Anthropic API error {{status}}: {{message}}', { status: e.status, message: e.message })
  return String(e instanceof Error ? e.message : e)
}

// ---------------------------------------------------------------------------
// Anthropic (official SDK, Messages API, streaming)
// ---------------------------------------------------------------------------
// Models that accept server-side refusal fallbacks ("default" routing).
const FALLBACK_MODELS = new Set(['claude-fable-5-1', 'claude-opus-5-5', 'claude-opus-5', 'claude-sonnet-5-5'])
const ADAPTIVE_ONLY = /^claude-(fable|opus-5|opus-4-[678]|sonnet-5|haiku-5|sonnet-4-6)/

class AnthropicSession implements LlmSession {
  private client: Anthropic
  private tools: Anthropic.Beta.BetaTool[]
  private lastInput: number | null = null // tokens the last request sent, as Anthropic counted them
  private sizeAtLast = 0 // the history's size (characters) when it was sent
  private pending = '' // a summary waiting for the next message, when nothing after it was kept

  constructor(private cfg: ProviderConfig, tools: ToolDef[], readonly system: string, private messages: Anthropic.Beta.BetaMessageParam[]) {
    this.client = new Anthropic({
      apiKey: cfg.apiKey,
      baseURL: cfg.baseUrl,
      dangerouslyAllowBrowser: true, // the user's own key, used from their own browser
    })
    this.tools = tools.map((t) => ({
      name: t.name,
      description: t.description,
      input_schema: jsonSchema(t) as Anthropic.Beta.BetaTool.InputSchema,
      eager_input_streaming: true, // stream large inputs (file bodies) as generated; validated in tools.ts
    }))
  }

  addUser(text: string) { this.messages.push({ role: 'user', content: this.pending + text }); this.pending = '' }
  save() { return this.messages }

  get length() { return this.messages.length }
  private size() { return JSON.stringify(this.messages).length }
  floor() { return tokensIn(this.system.length + JSON.stringify(this.tools).length) }
  used() { return this.lastInput !== null ? this.lastInput + tokensIn(Math.max(0, this.size() - this.sizeAtLast)) : this.floor() + tokensIn(this.size()) }
  turnStarts() { return this.messages.flatMap((m, i) => (m.role === 'user' && typeof m.content === 'string' ? [i] : [])) }

  transcript(from: number, to: number, maxResult: number) {
    const out: string[] = []
    for (const m of this.messages.slice(from, to)) {
      if (typeof m.content === 'string') { out.push(`${m.role === 'user' ? 'User' : 'Assistant'}: ${m.content}`); continue }
      for (const b of m.content as any[]) {
        if (b.type === 'text' && b.text) out.push(`${m.role === 'user' ? 'User' : 'Assistant'}: ${b.text}`)
        else if (b.type === 'tool_use') out.push(`Assistant used ${b.name}: ${cut(JSON.stringify(b.input), 600)}`)
        else if (b.type === 'tool_result') out.push(`Result${b.is_error ? ' (error)' : ''}: ${cut(typeof b.content === 'string' ? b.content : JSON.stringify(b.content), maxResult)}`)
      }
    }
    return out.join('\n\n')
  }

  /** Thinking blocks are valid only after the exact history they came from: after an edit, none can stay. */
  forgetThinking() { this.dropThinking() }
  private dropThinking() {
    for (const m of this.messages) {
      if (m.role !== 'assistant' || typeof m.content === 'string') continue
      const kept = (m.content as any[]).filter((b) => b.type !== 'thinking' && b.type !== 'redacted_thinking')
      m.content = kept.length ? kept : [{ type: 'text', text: '…' }]
    }
  }

  replaceBefore(keepFrom: number, summary: string) {
    this.messages = this.messages.slice(keepFrom)
    const first = this.messages[0]
    if (first && first.role === 'user' && typeof first.content === 'string') first.content = summaryNote(summary) + first.content
    else this.pending = summaryNote(summary)
    this.dropThinking()
    this.lastInput = null
  }

  shrinkResults(from: number, max: number) {
    const rounds = this.messages.map((m, i) => (i >= from && m.role === 'user' && Array.isArray(m.content) ? i : -1)).filter((i) => i >= 0)
    let changed = false
    for (const i of rounds.slice(0, -1)) {
      for (const b of this.messages[i].content as any[]) {
        if (b.type === 'tool_result' && typeof b.content === 'string' && b.content.length > max) { b.content = cut(b.content, max); changed = true }
      }
    }
    if (changed) { this.dropThinking(); this.lastInput = null }
    return changed
  }

  async ask(system: string, prompt: string, signal: AbortSignal) {
    const params: any = { model: this.cfg.model, max_tokens: 16000, system, messages: [{ role: 'user', content: prompt }] }
    if (ADAPTIVE_ONLY.test(this.cfg.model)) params.output_config = { effort: 'low' }
    const msg = await this.client.messages.stream(params, { signal }).finalMessage()
    if (msg.stop_reason === 'refusal') throw new Error(t('The model declined to summarise the conversation.'))
    return msg.content.flatMap((b) => (b.type === 'text' ? [b.text] : [])).join('\n').trim()
  }

  async contextWindow() {
    try { return (await this.client.models.retrieve(this.cfg.model)).max_input_tokens ?? null } catch { return null }
  }

  addToolResults(results: { id: string; content: string; isError: boolean }[]) {
    // All results for one assistant turn go back in a single user message.
    this.messages.push({
      role: 'user',
      content: results.map((r) => ({ type: 'tool_result' as const, tool_use_id: r.id, content: r.content, ...(r.isError ? { is_error: true } : {}) })),
    })
  }

  async turn(ev: TurnEvents, signal: AbortSignal): Promise<TurnResult> {
    const model = this.cfg.model
    const params: Anthropic.Beta.MessageCreateParamsNonStreaming = {
      model,
      max_tokens: 64000,
      system: this.system,
      messages: this.messages,
      tools: this.tools,
      cache_control: { type: 'ephemeral' }, // reuse the cached prefix across the tool loop
    }
    if (ADAPTIVE_ONLY.test(model)) {
      params.thinking = { type: 'adaptive', display: 'summarized' }
      params.output_config = { effort: this.cfg.effort }
    }
    if (FALLBACK_MODELS.has(model)) {
      // If a safety classifier declines, re-run on Anthropic's recommended fallback model.
      params.betas = ['server-side-fallback-2026-07-01']
      params.fallbacks = 'default'
    }

    let message: Anthropic.Beta.BetaMessage | null = null
    const sent = this.size()
    for (let attempt = 0; !message; attempt++) {
      const stream = this.client.beta.messages.stream(params, { signal })
      try {
        for await (const e of stream) {
          if (e.type === 'content_block_start' && e.content_block.type === 'tool_use') ev.toolStart(e.content_block.name)
          else if (e.type === 'content_block_delta') {
            if (e.delta.type === 'text_delta') ev.text(e.delta.text)
            else if (e.delta.type === 'thinking_delta') ev.thinking(e.delta.thinking)
          }
        }
        message = await stream.finalMessage()
      } catch (err) {
        // Only an unparseable streamed tool input is worth re-issuing; API errors propagate.
        if (err instanceof Anthropic.APIError || signal.aborted || attempt >= 2) throw err
        ev.text(`\n_(${t('retrying: the model produced malformed tool input')})_\n`)
      }
    }

    const u = message.usage
    this.lastInput = u.input_tokens + (u.cache_read_input_tokens ?? 0) + (u.cache_creation_input_tokens ?? 0)
    this.sizeAtLast = sent
    if (message.stop_reason === 'refusal') {
      // Discard the partial turn: it is not appended, so history stays valid.
      const category = message.stop_details?.category
      return { stop: 'refusal', toolCalls: [], note: category ? t('The model declined this request ({{category}}).', { category }) : t('The model declined this request.') }
    }
    const toolCalls = message.content
      .filter((b): b is Anthropic.Beta.BetaToolUseBlock => b.type === 'tool_use')
      .map((b) => ({ id: b.id, name: b.name, input: b.input }))
    if (message.stop_reason === 'max_tokens' && toolCalls.length) {
      // A truncated tool input can still parse; never run it, and don't append the turn.
      return { stop: 'max_tokens', toolCalls: [], note: t('The response was cut off mid tool call (max_tokens).') }
    }
    this.messages.push({ role: 'assistant', content: echoable(message.content) })
    if (message.stop_reason === 'pause_turn') return this.turn(ev, signal)
    return { stop: toolCalls.length ? 'tool_use' : message.stop_reason === 'max_tokens' ? 'max_tokens' : 'end', toolCalls }
  }
}

/** After a mid-output fallback, blocks before the last `fallback` marker that the next
 *  model can't continue from (thinking, tool_use) are omitted when echoing back. */
function echoable(content: Anthropic.Beta.BetaContentBlock[]): Anthropic.Beta.BetaContentBlockParam[] {
  const last = content.map((b) => b.type).lastIndexOf('fallback' as any)
  if (last < 0) return content as Anthropic.Beta.BetaContentBlockParam[]
  return content.filter((b, i) => i > last || !['thinking', 'redacted_thinking', 'tool_use', 'server_tool_use'].includes(b.type)) as Anthropic.Beta.BetaContentBlockParam[]
}

// ---------------------------------------------------------------------------
// OpenAI-compatible Chat Completions (OpenAI, OpenRouter, LM Studio, Ollama, vLLM…)
// ---------------------------------------------------------------------------
type OaiMessage =
  | { role: 'system' | 'user'; content: string }
  | { role: 'assistant'; content: string | null; tool_calls?: { id: string; type: 'function'; function: { name: string; arguments: string } }[] }
  | { role: 'tool'; tool_call_id: string; content: string }

/** Servers that refused `stream_options` (asking for token counts): not asked again. */
const NO_USAGE = new Set<string>()

class OpenAISession implements LlmSession {
  private messages: OaiMessage[] // without the system prompt, which is sent first each time
  private tools: unknown[]
  private lastInput: number | null = null
  private sizeAtLast = 0
  private pending = ''

  constructor(private cfg: ProviderConfig, tools: ToolDef[], readonly system: string, history: OaiMessage[]) {
    this.messages = history.filter((m) => m.role !== 'system')
    this.tools = tools.map((t) => ({ type: 'function', function: { name: t.name, description: t.description, parameters: jsonSchema(t) } }))
  }

  addUser(text: string) { this.messages.push({ role: 'user', content: this.pending + text }); this.pending = '' }
  save() { return this.messages } // today's system prompt is used when it's picked up again
  addToolResults(results: { id: string; content: string; isError: boolean }[]) {
    for (const r of results) this.messages.push({ role: 'tool', tool_call_id: r.id, content: r.isError ? `ERROR: ${r.content}` : r.content })
  }

  get length() { return this.messages.length }
  private size() { return JSON.stringify(this.messages).length }
  floor() { return tokensIn(this.system.length + JSON.stringify(this.tools).length) }
  used() { return this.lastInput !== null ? this.lastInput + tokensIn(Math.max(0, this.size() - this.sizeAtLast)) : this.floor() + tokensIn(this.size()) }
  turnStarts() { return this.messages.flatMap((m, i) => (m.role === 'user' ? [i] : [])) }

  transcript(from: number, to: number, maxResult: number) {
    const out: string[] = []
    for (const m of this.messages.slice(from, to)) {
      if (m.role === 'user') out.push(`User: ${m.content}`)
      else if (m.role === 'tool') out.push(`Result: ${cut(m.content, maxResult)}`)
      else if (m.role === 'assistant') {
        if (m.content) out.push(`Assistant: ${m.content}`)
        for (const c of m.tool_calls ?? []) out.push(`Assistant used ${c.function.name}: ${cut(c.function.arguments, 600)}`)
      }
    }
    return out.join('\n\n')
  }

  forgetThinking() {} // reasoning isn't kept in this format

  replaceBefore(keepFrom: number, summary: string) {
    this.messages = this.messages.slice(keepFrom)
    const first = this.messages[0]
    if (first?.role === 'user') first.content = summaryNote(summary) + first.content
    else this.pending = summaryNote(summary)
    this.lastInput = null
  }

  shrinkResults(from: number, max: number) {
    // the newest round is the tool messages after the last assistant message
    const lastCall = this.messages.map((m) => m.role).lastIndexOf('assistant')
    let changed = false
    for (let i = from; i < lastCall; i++) {
      const m = this.messages[i]
      if (m.role === 'tool' && m.content.length > max) { m.content = cut(m.content, max); changed = true }
    }
    if (changed) this.lastInput = null
    return changed
  }

  private base() { return this.cfg.baseUrl.replace(/\/$/, '') }
  private headers(): Record<string, string> {
    return { 'Content-Type': 'application/json', ...(this.cfg.apiKey ? { Authorization: `Bearer ${this.cfg.apiKey}` } : {}) }
  }

  async ask(system: string, prompt: string, signal: AbortSignal) {
    const res = await fetch(`${this.base()}/chat/completions`, {
      method: 'POST', signal, headers: this.headers(),
      body: JSON.stringify({ model: this.cfg.model, messages: [{ role: 'system', content: system }, { role: 'user', content: prompt }], stream: false }),
    })
    if (!res.ok) throw new Error(t('{{provider}} returned HTTP {{status}}: {{body}}', { provider: this.cfg.label, status: res.status, body: (await res.text().catch(() => '')).slice(0, 400) }))
    const j = await res.json()
    return String(j.choices?.[0]?.message?.content ?? '').replace(/<think>[\s\S]*?<\/think>/g, '').trim()
  }

  async contextWindow() {
    const model = this.cfg.model
    if (isLocal(this.base())) {
      const root = this.base().replace(/\/v1$/, '')
      // LM Studio: only the context it loaded the model with counts (its maximum is often far more);
      // before the model is loaded there's nothing to go on yet
      const lm = await getJson(`${root}/api/v0/models/${encodeURIComponent(model)}`)
      if (lm?.state) return lm.state === 'loaded' && lm.loaded_context_length ? Number(lm.loaded_context_length) : null
      const ps = await getJson(`${root}/api/ps`) // Ollama: models loaded right now
      const running = ps?.models?.find((m: any) => m.name === model || m.model === model)
      if (running?.context_length) return Number(running.context_length)
      const show = await getJson(`${root}/api/show`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model }) })
      if (show) return Number(/num_ctx\s+(\d+)/.exec(show.parameters ?? '')?.[1] ?? OLLAMA_DEFAULT_CONTEXT)
      return null
    }
    const list = await getJson(`${this.base()}/models`, { headers: this.headers() }) // OpenRouter lists each model's context
    const hit = list?.data?.find((m: any) => m.id === model)
    return hit?.context_length ? Number(hit.context_length) : null
  }

  async turn(ev: TurnEvents, signal: AbortSignal): Promise<TurnResult> {
    const sent = this.size()
    const request = (usage: boolean) => fetch(`${this.base()}/chat/completions`, {
      method: 'POST',
      signal,
      headers: this.headers(),
      body: JSON.stringify({ model: this.cfg.model, messages: [{ role: 'system', content: this.system }, ...this.messages], tools: this.tools, stream: true,
        ...(usage ? { stream_options: { include_usage: true } } : {}) }),
    })
    let res = await request(!NO_USAGE.has(this.base()))
    if (res.status === 400 && !NO_USAGE.has(this.base())) {
      const body = await res.clone().text().catch(() => '')
      if (/stream_options|include_usage/i.test(body)) { NO_USAGE.add(this.base()); res = await request(false) }
    }
    if (!res.ok || !res.body) {
      const body = await res.text().catch(() => '')
      throw new Error(t('{{provider}} returned HTTP {{status}}: {{body}}', { provider: this.cfg.label, status: res.status, body: body.slice(0, 400) }))
    }
    let text = ''
    let finish = ''
    const calls: { id: string; name: string; args: string }[] = []
    const reader = res.body.pipeThrough(new TextDecoderStream()).getReader()
    let buf = ''
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break
      buf += value
      let nl: number
      while ((nl = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, nl).trim()
        buf = buf.slice(nl + 1)
        if (!line.startsWith('data:')) continue
        const data = line.slice(5).trim()
        if (data === '[DONE]') continue
        let j: any
        try { j = JSON.parse(data) } catch { continue }
        if (j.error) throw new Error(`${this.cfg.label}: ${j.error.message ?? JSON.stringify(j.error)}`)
        if (j.usage?.prompt_tokens) { this.lastInput = Number(j.usage.prompt_tokens); this.sizeAtLast = sent }
        const ch = j.choices?.[0]
        if (!ch) continue
        const d = ch.delta ?? {}
        if (d.content) { text += d.content; ev.text(d.content) }
        const reasoning = d.reasoning_content ?? d.reasoning
        if (typeof reasoning === 'string' && reasoning) ev.thinking(reasoning)
        for (const tc of d.tool_calls ?? []) {
          const i = tc.index ?? calls.length
          if (!calls[i]) { calls[i] = { id: tc.id ?? `call_${i}_${Date.now()}`, name: '', args: '' }; }
          if (tc.id) calls[i].id = tc.id
          if (tc.function?.name) { calls[i].name += tc.function.name; ev.toolStart(calls[i].name) }
          if (tc.function?.arguments) calls[i].args += tc.function.arguments
        }
        if (ch.finish_reason) finish = ch.finish_reason
      }
    }
    const done = calls.filter(Boolean)
    if (finish === 'length' && done.length) return { stop: 'max_tokens', toolCalls: [], note: t('The response was cut off mid tool call.') }
    this.messages.push({
      role: 'assistant',
      content: text || null,
      ...(done.length ? { tool_calls: done.map((c) => ({ id: c.id, type: 'function' as const, function: { name: c.name, arguments: c.args || '{}' } })) } : {}),
    })
    const toolCalls = done.map((c) => {
      let input: unknown
      try { input = JSON.parse(c.args || '{}') } catch { input = undefined }
      return { id: c.id, name: c.name, input, rawInput: c.args }
    })
    return { stop: toolCalls.length ? 'tool_use' : finish === 'length' ? 'max_tokens' : 'end', toolCalls }
  }
}
