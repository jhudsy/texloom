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
}

export function createSession(cfg: ProviderConfig, tools: ToolDef[], system: string): LlmSession {
  return cfg.kind === 'anthropic' ? new AnthropicSession(cfg, tools, system) : new OpenAISession(cfg, tools, system)
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
  private messages: Anthropic.Beta.BetaMessageParam[] = []
  private tools: Anthropic.Beta.BetaTool[]

  constructor(private cfg: ProviderConfig, tools: ToolDef[], private system: string) {
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

  addUser(text: string) { this.messages.push({ role: 'user', content: text }) }

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

class OpenAISession implements LlmSession {
  private messages: OaiMessage[]
  private tools: unknown[]

  constructor(private cfg: ProviderConfig, tools: ToolDef[], system: string) {
    this.messages = [{ role: 'system', content: system }]
    this.tools = tools.map((t) => ({ type: 'function', function: { name: t.name, description: t.description, parameters: jsonSchema(t) } }))
  }

  addUser(text: string) { this.messages.push({ role: 'user', content: text }) }
  addToolResults(results: { id: string; content: string; isError: boolean }[]) {
    for (const r of results) this.messages.push({ role: 'tool', tool_call_id: r.id, content: r.isError ? `ERROR: ${r.content}` : r.content })
  }

  async turn(ev: TurnEvents, signal: AbortSignal): Promise<TurnResult> {
    const res = await fetch(`${this.cfg.baseUrl.replace(/\/$/, '')}/chat/completions`, {
      method: 'POST',
      signal,
      headers: { 'Content-Type': 'application/json', ...(this.cfg.apiKey ? { Authorization: `Bearer ${this.cfg.apiKey}` } : {}) },
      body: JSON.stringify({ model: this.cfg.model, messages: this.messages, tools: this.tools, stream: true }),
    })
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
