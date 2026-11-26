import type { Catalog } from '../../i18n'

const de: Catalog = {
  'Get a key at console.anthropic.com. The key is sent straight from this browser to Anthropic.': 'Einen Schlüssel erhalten Sie unter console.anthropic.com. Er wird direkt von diesem Browser an Anthropic gesendet.',
  'Pick a model that supports tool (function) calling.': 'Wählen Sie ein Modell, das Tool-Aufrufe (Function Calling) unterstützt.',
  'Self-hosted': 'Selbst gehostet',
  'Ollama, LM Studio, vLLM, llama.cpp, LiteLLM or any server with the OpenAI API, on this computer or another. Leave the address empty to find Ollama or LM Studio on this computer. The server must allow requests from this site (CORS): `lms server start --cors` for LM Studio, `OLLAMA_ORIGINS` for Ollama. Browsers don’t let this page reach plain http on another machine: give that server an https address, or reach it through an SSH tunnel (`ssh -L 11434:localhost:11434 your-server`) and leave the address empty. Pick a model that supports tools.': 'Ollama, LM Studio, vLLM, llama.cpp, LiteLLM oder jeder Server mit der OpenAI-API, auf diesem Computer oder einem anderen. Lassen Sie die Adresse leer, um Ollama oder LM Studio auf diesem Computer zu finden. Der Server muss Anfragen von dieser Website erlauben (CORS): `lms server start --cors` für LM Studio, `OLLAMA_ORIGINS` für Ollama. Browser lassen diese Seite kein einfaches http auf einem anderen Rechner erreichen: Geben Sie diesem Server eine https-Adresse oder erreichen Sie ihn über einen SSH-Tunnel (`ssh -L 11434:localhost:11434 your-server`) und lassen Sie die Adresse leer. Wählen Sie ein Modell, das Tools unterstützt.',
}

export default de
