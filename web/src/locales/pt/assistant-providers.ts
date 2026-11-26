import type { Catalog } from '../../i18n'

const pt: Catalog = {
  'Get a key at console.anthropic.com. The key is sent straight from this browser to Anthropic.': 'Obtenha uma chave em console.anthropic.com. A chave é enviada diretamente deste navegador para a Anthropic.',
  'Pick a model that supports tool (function) calling.': 'Escolha um modelo que suporte chamadas de ferramentas (funções).',
  'Self-hosted': 'Auto-alojado',
  'Ollama, LM Studio, vLLM, llama.cpp, LiteLLM or any server with the OpenAI API, on this computer or another. Leave the address empty to find Ollama or LM Studio on this computer. The server must allow requests from this site (CORS): `lms server start --cors` for LM Studio, `OLLAMA_ORIGINS` for Ollama. Browsers don’t let this page reach plain http on another machine: give that server an https address, or reach it through an SSH tunnel (`ssh -L 11434:localhost:11434 your-server`) and leave the address empty. Pick a model that supports tools.': 'Ollama, LM Studio, vLLM, llama.cpp, LiteLLM ou qualquer servidor com a API da OpenAI, neste computador ou noutro. Deixe o endereço vazio para encontrar o Ollama ou o LM Studio neste computador. O servidor tem de permitir pedidos deste site (CORS): `lms server start --cors` para o LM Studio, `OLLAMA_ORIGINS` para o Ollama. Os navegadores não deixam esta página aceder a http simples noutra máquina: dê a esse servidor um endereço https, ou aceda-lhe por um túnel SSH (`ssh -L 11434:localhost:11434 your-server`) e deixe o endereço vazio. Escolha um modelo que suporte ferramentas.',
}

export default pt
