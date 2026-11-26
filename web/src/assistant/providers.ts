// LLM provider configuration. Stored only in this browser (localStorage) and never
// synced into the project: API keys must not travel with a share link.

import { t } from '../i18n'
import { getString, readJson, setString, writeJson } from '../store'
import { anthropicModels } from './llm'
import { quiet } from '../quiet'

export type Kind = 'anthropic' | 'openai'
export type Effort = 'low' | 'medium' | 'high' | 'xhigh' | 'max'

export interface ProviderConfig {
  kind: Kind
  label: string
  baseUrl: string // for Self-hosted, empty until it's found on this computer or given
  apiKey: string
  model: string
  effort: Effort // Anthropic only
  models: string[] // the provider's models, as it last listed them
  help?: string
  /** The model's context window in tokens, when the person sets it; otherwise it's asked of the provider. */
  contextWindow?: number
  /** Self-hosted: set up (a server was found or given). The others are set up when they have a key. */
  on?: boolean
}

export const PRESETS: Record<string, ProviderConfig> = {
  anthropic: {
    kind: 'anthropic', label: 'Anthropic (Claude)', baseUrl: 'https://api.anthropic.com', apiKey: '',
    model: '', effort: 'high', models: [], // (the models are Anthropic's to list: asked once there's a key)
    help: t('Get a key at console.anthropic.com. The key is sent straight from this browser to Anthropic.'),
  },
  openai: {
    kind: 'openai', label: 'OpenAI', baseUrl: 'https://api.openai.com/v1', apiKey: '', model: '', effort: 'high', models: [],
    help: t('Pick a model that supports tool (function) calling.'),
  },
  openrouter: {
    kind: 'openai', label: 'OpenRouter', baseUrl: 'https://openrouter.ai/api/v1', apiKey: '', model: '', effort: 'high', models: [],
    help: t('Pick a model that supports tool (function) calling.'),
  },
  selfhosted: {
    kind: 'openai', label: t('Self-hosted'), baseUrl: '', apiKey: '', model: '', effort: 'high', models: [],
    help: t('Ollama, LM Studio, vLLM, llama.cpp, LiteLLM or any server with the OpenAI API, on this computer or another. Leave the address empty to find Ollama or LM Studio on this computer. The server must allow requests from this site (CORS): `lms server start --cors` for LM Studio, `OLLAMA_ORIGINS` for Ollama. Browsers don’t let this page reach plain http on another machine: give that server an https address, or reach it through an SSH tunnel (`ssh -L 11434:localhost:11434 your-server`) and leave the address empty. Pick a model that supports tools.'),
  },
}
/** Online providers first, then your own. */
export const PROVIDERS = ['anthropic', 'openai', 'openrouter', 'selfhosted']
/** Where Self-hosted looks on this computer when no address is given: Ollama, then LM Studio. */
const ON_THIS_COMPUTER = ['http://localhost:11434/v1', 'http://localhost:1234/v1']
/** Providers there used to be, now Self-hosted (settings and past conversations still name them). */
const WAS_SELFHOSTED = ['lmstudio', 'ollama', 'custom']
const OLD_ADDRESS: Record<string, string> = { lmstudio: 'http://localhost:1234/v1', ollama: 'http://localhost:11434/v1', custom: 'http://localhost:8000/v1' }
/** A provider as it's known now. */
export const providerOf = (id: string): string => (WAS_SELFHOSTED.includes(id) ? 'selfhosted' : id)

type Saved = { active: string; configs: Record<string, Partial<ProviderConfig>> }
const KEY = 'texloom.ai'

function load(): Saved {
  const s: Saved = { active: 'anthropic', configs: {}, ...readJson(KEY, {}) }
  // LM Studio, Ollama and Other became Self-hosted: the one in use (else one set up) carries over
  const old = WAS_SELFHOSTED.filter((id) => s.configs[id])
  if (old.length && !s.configs.selfhosted) {
    const id = old.includes(s.active) ? s.active : old.find((o) => s.configs[o]!.model) ?? old[0]
    const c = s.configs[id]!
    s.configs.selfhosted = { baseUrl: c.baseUrl || OLD_ADDRESS[id], apiKey: c.apiKey ?? '', model: c.model ?? '', models: c.models ?? [], contextWindow: c.contextWindow, on: true }
  }
  for (const id of old) delete s.configs[id]
  s.active = providerOf(s.active)
  if (old.length) writeJson(KEY, s)
  return s
}

export const aiPrefs: {
  activeId: string
  get(id?: string): ProviderConfig
  /** Whether it's set up: has a key, or (Self-hosted) a server. */
  setUp(id: string): boolean
  save(id: string, cfg: Partial<ProviderConfig>): void
  reviewEdits: boolean
  /** Keep past conversations in this browser (on unless turned off here). */
  keepChats: boolean
  /** The person's own standing instructions, sent to every model with the assistant's. */
  instructions: string
} = {
  get activeId(): string { const a = load().active; return a in PRESETS ? a : 'anthropic' },
  set activeId(id: string) { const s = load(); s.active = id; writeJson(KEY, s) },
  get(id = aiPrefs.activeId): ProviderConfig {
    const saved = load().configs[id] ?? {}
    return { ...PRESETS[id], ...saved, models: saved.models ?? [] }
  },
  setUp(id: string): boolean {
    const c = aiPrefs.get(id)
    return id === 'selfhosted' ? !!c.on && !!c.baseUrl : !!c.apiKey
  },
  save(id: string, cfg: Partial<ProviderConfig>) {
    const s = load()
    s.configs[id] = { ...s.configs[id], ...cfg }
    writeJson(KEY, s)
  },
  get reviewEdits(): boolean { return getString('texloom.ai.review') !== 'off' },
  set reviewEdits(v: boolean) { setString('texloom.ai.review', v ? 'on' : 'off') },
  get keepChats(): boolean { return getString('texloom.ai.keep') !== 'off' },
  set keepChats(v: boolean) { setString('texloom.ai.keep', v ? 'on' : 'off') },
  get instructions(): string { return getString('texloom.ai.instructions') ?? '' },
  set instructions(v: string) { setString('texloom.ai.instructions', v.trim() ? v : null) },
}

/** Find a model server on this computer (Ollama, LM Studio): its address and models, or null. */
export async function findOnThisComputer(apiKey = ''): Promise<{ baseUrl: string; models: string[] } | null> {
  for (const baseUrl of ON_THIS_COMPUTER) {
    try { return { baseUrl, models: await listModels({ ...PRESETS.selfhosted, baseUrl, apiKey }) } }
    catch (e) { quiet(`no model server at ${baseUrl}: the next place is tried`, e) }
  }
  return null
}

/** List the models a provider offers: Anthropic's own list, or an OpenAI-compatible /models endpoint. */
export async function listModels(cfg: ProviderConfig): Promise<string[]> {
  if (cfg.kind === 'anthropic') return anthropicModels(cfg)
  const r = await fetch(`${cfg.baseUrl.replace(/\/$/, '')}/models`, {
    headers: cfg.apiKey ? { Authorization: `Bearer ${cfg.apiKey}` } : {},
  })
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  const j = await r.json()
  return (j.data ?? []).map((m: any) => m.id).filter((id: string) => !/embed/i.test(id)).sort()
}
