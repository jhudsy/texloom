import type { Catalog } from '../../i18n'

const de: Catalog = {
  'Get a key at console.anthropic.com. The key is sent straight from this browser to Anthropic.': 'Einen Schlüssel erhalten Sie unter console.anthropic.com. Er wird direkt von diesem Browser an Anthropic gesendet.',
  'Any model with tool (function) calling. Use “Load models” to list them.': 'Jedes Modell mit Werkzeug- (Funktions-)Aufrufen. Mit „Modelle laden“ werden sie aufgelistet.',
  'LM Studio (local)': 'LM Studio (lokal)',
  'Start the server with CORS enabled: `lms server start --cors` (or Developer → Settings → Enable CORS). Pick a model that supports tool use.': 'Starten Sie den Server mit aktiviertem CORS: `lms server start --cors` (oder Developer → Settings → Enable CORS). Wählen Sie ein Modell, das Werkzeuge unterstützt.',
  'Ollama (local)': 'Ollama (lokal)',
  'Works from localhost out of the box. From another origin, set OLLAMA_ORIGINS to this site’s address. Pick a model that supports tools (e.g. qwen2.5, llama3.1).': 'Funktioniert von localhost aus sofort. Von einer anderen Herkunft setzen Sie OLLAMA_ORIGINS auf die Adresse dieser Website. Wählen Sie ein Modell, das Werkzeuge unterstützt (z. B. qwen2.5, llama3.1).',
  'Other (OpenAI-compatible)': 'Sonstige (OpenAI-kompatibel)',
  'vLLM, llama.cpp server, LiteLLM, or any /v1/chat/completions endpoint with tool calling.': 'vLLM, llama.cpp-Server, LiteLLM oder ein beliebiger /v1/chat/completions-Endpunkt mit Werkzeugaufrufen.',
}

export default de
