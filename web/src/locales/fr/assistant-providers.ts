import type { Catalog } from '../../i18n'
const fr: Catalog = {
  'Get a key at console.anthropic.com. The key is sent straight from this browser to Anthropic.':
    'Obtenez une clé sur console.anthropic.com. Elle est envoyée directement de ce navigateur à Anthropic.',
  'Any model with tool (function) calling. Use “Load models” to list them.':
    'Tout modèle d’IA qui prend en charge l’appel d’outils (fonctions). Utilisez « Charger les modèles » pour les lister.',
  'LM Studio (local)': 'LM Studio (local)',
  'Start the server with CORS enabled: `lms server start --cors` (or Developer → Settings → Enable CORS). Pick a model that supports tool use.':
    'Lancez le serveur avec CORS activé : `lms server start --cors` (ou Developer → Settings → Enable CORS). Choisissez un modèle d’IA qui prend en charge les outils.',
  'Ollama (local)': 'Ollama (local)',
  'Works from localhost out of the box. From another origin, set OLLAMA_ORIGINS to this site’s address. Pick a model that supports tools (e.g. qwen2.5, llama3.1).':
    'Fonctionne tel quel depuis localhost. Depuis une autre origine, définissez OLLAMA_ORIGINS avec l’adresse de ce site. Choisissez un modèle d’IA qui prend en charge les outils (p. ex. qwen2.5, llama3.1).',
  'Other (OpenAI-compatible)': 'Autre (compatible OpenAI)',
  'vLLM, llama.cpp server, LiteLLM, or any /v1/chat/completions endpoint with tool calling.':
    'vLLM, serveur llama.cpp, LiteLLM, ou tout point de terminaison /v1/chat/completions avec appel d’outils.',
}
export default fr
