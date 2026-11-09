import type { Catalog } from '../../i18n'

const pt: Catalog = {
  'Get a key at console.anthropic.com. The key is sent straight from this browser to Anthropic.': 'Obtenha uma chave em console.anthropic.com. A chave é enviada diretamente deste navegador para a Anthropic.',
  'Any model with tool (function) calling. Use “Load models” to list them.': 'Qualquer modelo com chamada de ferramentas (funções). Use “Carregar modelos” para listá-los.',
  'LM Studio (local)': 'LM Studio (local)',
  'Start the server with CORS enabled: `lms server start --cors` (or Developer → Settings → Enable CORS). Pick a model that supports tool use.': 'Inicie o servidor com CORS ativado: `lms server start --cors` (ou Developer → Settings → Enable CORS). Escolha um modelo compatível com ferramentas.',
  'Ollama (local)': 'Ollama (local)',
  'Works from localhost out of the box. From another origin, set OLLAMA_ORIGINS to this site’s address. Pick a model that supports tools (e.g. qwen2.5, llama3.1).': 'Funciona direto em localhost. De outra origem, defina OLLAMA_ORIGINS com o endereço deste site. Escolha um modelo compatível com ferramentas (ex.: qwen2.5, llama3.1).',
  'Other (OpenAI-compatible)': 'Outro (compatível com OpenAI)',
  'vLLM, llama.cpp server, LiteLLM, or any /v1/chat/completions endpoint with tool calling.': 'vLLM, servidor llama.cpp, LiteLLM ou qualquer endpoint /v1/chat/completions com chamada de ferramentas.',
}

export default pt
