import type { Catalog } from '../../i18n'

const es: Catalog = {
  'Get a key at console.anthropic.com. The key is sent straight from this browser to Anthropic.': 'Consigue una clave en console.anthropic.com. La clave se envía directamente de este navegador a Anthropic.',
  'Any model with tool (function) calling. Use “Load models” to list them.': 'Cualquier modelo con llamadas a herramientas (funciones). Usa «Cargar modelos» para verlos.',
  'LM Studio (local)': 'LM Studio (local)',
  'Start the server with CORS enabled: `lms server start --cors` (or Developer → Settings → Enable CORS). Pick a model that supports tool use.': 'Inicia el servidor con CORS activado: `lms server start --cors` (o Developer → Settings → Enable CORS). Elige un modelo que admita herramientas.',
  'Ollama (local)': 'Ollama (local)',
  'Works from localhost out of the box. From another origin, set OLLAMA_ORIGINS to this site’s address. Pick a model that supports tools (e.g. qwen2.5, llama3.1).': 'Funciona directamente desde localhost. Desde otro origen, define OLLAMA_ORIGINS con la dirección de este sitio. Elige un modelo que admita herramientas (p. ej., qwen2.5, llama3.1).',
  'Other (OpenAI-compatible)': 'Otro (compatible con OpenAI)',
  'vLLM, llama.cpp server, LiteLLM, or any /v1/chat/completions endpoint with tool calling.': 'vLLM, servidor de llama.cpp, LiteLLM o cualquier punto de acceso /v1/chat/completions con llamadas a herramientas.',
}

export default es
