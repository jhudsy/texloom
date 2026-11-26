import type { Catalog } from '../../i18n'

const zh: Catalog = {
  'Get a key at console.anthropic.com. The key is sent straight from this browser to Anthropic.': '请在 console.anthropic.com 获取密钥。密钥由此浏览器直接发送给 Anthropic。',
  'Pick a model that supports tool (function) calling.': '请选择支持工具（函数）调用的模型。',
  'Self-hosted': '自托管',
  'Ollama, LM Studio, vLLM, llama.cpp, LiteLLM or any server with the OpenAI API, on this computer or another. Leave the address empty to find Ollama or LM Studio on this computer. The server must allow requests from this site (CORS): `lms server start --cors` for LM Studio, `OLLAMA_ORIGINS` for Ollama. Browsers don’t let this page reach plain http on another machine: give that server an https address, or reach it through an SSH tunnel (`ssh -L 11434:localhost:11434 your-server`) and leave the address empty. Pick a model that supports tools.': 'Ollama、LM Studio、vLLM、llama.cpp、LiteLLM 或任何提供 OpenAI API 的服务器，可以在本机，也可以在另一台机器上。地址留空即可在本机查找 Ollama 或 LM Studio。服务器必须允许来自本站的请求（CORS）：LM Studio 用 `lms server start --cors`，Ollama 设置 `OLLAMA_ORIGINS`。浏览器不允许本页访问另一台机器上的普通 http：请给该服务器配置 https 地址，或通过 SSH 隧道（`ssh -L 11434:localhost:11434 your-server`）访问并将地址留空。请选择支持工具调用的模型。',
}

export default zh
