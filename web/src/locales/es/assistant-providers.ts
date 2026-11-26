import type { Catalog } from '../../i18n'

const es: Catalog = {
  'Get a key at console.anthropic.com. The key is sent straight from this browser to Anthropic.': 'Consigue una clave en console.anthropic.com. La clave se envía directamente de este navegador a Anthropic.',
  'Pick a model that supports tool (function) calling.': 'Elija un modelo que admita llamadas a herramientas (funciones).',
  'Self-hosted': 'Autoalojado',
  'Ollama, LM Studio, vLLM, llama.cpp, LiteLLM or any server with the OpenAI API, on this computer or another. Leave the address empty to find Ollama or LM Studio on this computer. The server must allow requests from this site (CORS): `lms server start --cors` for LM Studio, `OLLAMA_ORIGINS` for Ollama. Browsers don’t let this page reach plain http on another machine: give that server an https address, or reach it through an SSH tunnel (`ssh -L 11434:localhost:11434 your-server`) and leave the address empty. Pick a model that supports tools.': 'Ollama, LM Studio, vLLM, llama.cpp, LiteLLM o cualquier servidor con la API de OpenAI, en este ordenador o en otro. Deje la dirección vacía para buscar Ollama o LM Studio en este ordenador. El servidor debe permitir solicitudes de este sitio (CORS): `lms server start --cors` para LM Studio, `OLLAMA_ORIGINS` para Ollama. Los navegadores no dejan que esta página acceda a http simple en otra máquina: dé a ese servidor una dirección https, o acceda a él mediante un túnel SSH (`ssh -L 11434:localhost:11434 your-server`) y deje la dirección vacía. Elija un modelo que admita herramientas.',
}

export default es
