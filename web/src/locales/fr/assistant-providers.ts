import type { Catalog } from '../../i18n'
const fr: Catalog = {
  'Get a key at console.anthropic.com. The key is sent straight from this browser to Anthropic.':
    'Obtenez une clé sur console.anthropic.com. Elle est envoyée directement de ce navigateur à Anthropic.',
  'Pick a model that supports tool (function) calling.': 'Choisissez un modèle qui prend en charge l’appel d’outils (fonctions).',
  'Self-hosted': 'Auto-hébergé',
  'Ollama, LM Studio, vLLM, llama.cpp, LiteLLM or any server with the OpenAI API, on this computer or another. Leave the address empty to find Ollama or LM Studio on this computer. The server must allow requests from this site (CORS): `lms server start --cors` for LM Studio, `OLLAMA_ORIGINS` for Ollama. Browsers don’t let this page reach plain http on another machine: give that server an https address, or reach it through an SSH tunnel (`ssh -L 11434:localhost:11434 your-server`) and leave the address empty. Pick a model that supports tools.': 'Ollama, LM Studio, vLLM, llama.cpp, LiteLLM ou tout serveur doté de l’API OpenAI, sur cet ordinateur ou un autre. Laissez l’adresse vide pour trouver Ollama ou LM Studio sur cet ordinateur. Le serveur doit autoriser les requêtes de ce site (CORS) : `lms server start --cors` pour LM Studio, `OLLAMA_ORIGINS` pour Ollama. Les navigateurs ne laissent pas cette page joindre du http simple sur une autre machine : donnez à ce serveur une adresse https, ou passez par un tunnel SSH (`ssh -L 11434:localhost:11434 your-server`) et laissez l’adresse vide. Choisissez un modèle qui prend en charge les outils.',
}
export default fr
