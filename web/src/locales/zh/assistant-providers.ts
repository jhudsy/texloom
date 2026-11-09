import type { Catalog } from '../../i18n'

const zh: Catalog = {
  'Get a key at console.anthropic.com. The key is sent straight from this browser to Anthropic.': '请在 console.anthropic.com 获取密钥。密钥由此浏览器直接发送给 Anthropic。',
  'Any model with tool (function) calling. Use “Load models” to list them.': '任何支持工具（函数）调用的模型。使用“加载模型”可列出模型。',
  'LM Studio (local)': 'LM Studio（本地）',
  'Start the server with CORS enabled: `lms server start --cors` (or Developer → Settings → Enable CORS). Pick a model that supports tool use.': '启动服务器时启用 CORS：`lms server start --cors`（或 Developer → Settings → Enable CORS）。请选择支持工具调用的模型。',
  'Ollama (local)': 'Ollama（本地）',
  'Works from localhost out of the box. From another origin, set OLLAMA_ORIGINS to this site’s address. Pick a model that supports tools (e.g. qwen2.5, llama3.1).': '在 localhost 上可直接使用。从其他来源访问时，请将 OLLAMA_ORIGINS 设为此网站的地址。请选择支持工具调用的模型（例如 qwen2.5、llama3.1）。',
  'Other (OpenAI-compatible)': '其他（兼容 OpenAI）',
  'vLLM, llama.cpp server, LiteLLM, or any /v1/chat/completions endpoint with tool calling.': 'vLLM、llama.cpp 服务器、LiteLLM，或任何支持工具调用的 /v1/chat/completions 接口。',
}

export default zh
