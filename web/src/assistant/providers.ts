// LLM provider configuration. Stored only in this browser (localStorage) and never
// synced into the project: API keys must not travel with a share link.

import { t } from '../i18n'

export type Kind = 'anthropic' | 'openai'
export type Effort = 'low' | 'medium' | 'high' | 'xhigh' | 'max'

export interface ProviderConfig {
  kind: Kind
  label: string
  baseUrl: string
  apiKey: string
  model: string
  effort: Effort // Anthropic only
  models: string[] // suggestions for the model picker
  help?: string
}

export const PRESETS: Record<string, ProviderConfig> = {
  anthropic: {
    kind: 'anthropic', label: 'Anthropic (Claude)', baseUrl: 'https://api.anthropic.com', apiKey: '',
    model: 'claude-opus-5-5', effort: 'high',
    models: ['claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5-5', 'claude-fable-5-1'],
    help: t('Get a key at console.anthropic.com. The key is sent straight from this browser to Anthropic.'),
  },
  openai: {
    kind: 'openai', label: 'OpenAI', baseUrl: 'https://api.openai.com/v1', apiKey: '', model: '', effort: 'high', models: [],
    help: t('Any model with tool (function) calling. Use “Load models” to list them.'),
  },
  openrouter: {
    kind: 'openai', label: 'OpenRouter', baseUrl: 'https://openrouter.ai/api/v1', apiKey: '', model: '', effort: 'high', models: [],
  },
  lmstudio: {
    kind: 'openai', label: t('LM Studio (local)'), baseUrl: 'http://localhost:1234/v1', apiKey: '', model: '', effort: 'high', models: [],
    help: t('Start the server with CORS enabled: `lms server start --cors` (or Developer → Settings → Enable CORS). Pick a model that supports tool use.'),
  },
  ollama: {
    kind: 'openai', label: t('Ollama (local)'), baseUrl: 'http://localhost:11434/v1', apiKey: '', model: '', effort: 'high', models: [],
    help: t('Works from localhost out of the box. From another origin, set OLLAMA_ORIGINS to this site’s address. Pick a model that supports tools (e.g. qwen2.5, llama3.1).'),
  },
  custom: {
    kind: 'openai', label: t('Other (OpenAI-compatible)'), baseUrl: 'http://localhost:8000/v1', apiKey: '', model: '', effort: 'high', models: [],
    help: t('vLLM, llama.cpp server, LiteLLM, or any /v1/chat/completions endpoint with tool calling.'),
  },
}

type Saved = { active: string; configs: Record<string, Partial<ProviderConfig>> }
const KEY = 'texloom.ai'

function load(): Saved {
  try { return { active: 'anthropic', configs: {}, ...JSON.parse(localStorage.getItem(KEY) || '{}') } } catch { return { active: 'anthropic', configs: {} } }
}

export const aiPrefs: {
  activeId: string
  get(id?: string): ProviderConfig
  save(id: string, cfg: Partial<ProviderConfig>): void
  reviewEdits: boolean
  /** Keep past conversations in this browser (on unless turned off here). */
  keepChats: boolean
} = {
  get activeId(): string { return load().active in PRESETS ? load().active : 'anthropic' },
  set activeId(id: string) { const s = load(); s.active = id; localStorage.setItem(KEY, JSON.stringify(s)) },
  get(id = aiPrefs.activeId): ProviderConfig {
    const saved = load().configs[id] ?? {}
    return { ...PRESETS[id], ...saved, models: saved.models?.length ? saved.models : PRESETS[id].models }
  },
  save(id: string, cfg: Partial<ProviderConfig>) {
    const s = load()
    s.configs[id] = { ...s.configs[id], ...cfg }
    localStorage.setItem(KEY, JSON.stringify(s))
  },
  get reviewEdits(): boolean { return localStorage.getItem('texloom.ai.review') !== 'off' },
  set reviewEdits(v: boolean) { localStorage.setItem('texloom.ai.review', v ? 'on' : 'off') },
  get keepChats(): boolean { try { return localStorage.getItem('texloom.ai.keep') !== 'off' } catch { return true } },
  set keepChats(v: boolean) { try { localStorage.setItem('texloom.ai.keep', v ? 'on' : 'off') } catch {} },
}

/** List models from an OpenAI-compatible /models endpoint. */
export async function listModels(cfg: ProviderConfig): Promise<string[]> {
  const r = await fetch(`${cfg.baseUrl.replace(/\/$/, '')}/models`, {
    headers: cfg.apiKey ? { Authorization: `Bearer ${cfg.apiKey}` } : {},
  })
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  const j = await r.json()
  return (j.data ?? []).map((m: any) => m.id).filter((id: string) => !/embed/i.test(id)).sort()
}
