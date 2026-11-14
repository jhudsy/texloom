import type { Catalog } from '../../i18n'

const zh: Catalog = {
  'Anthropic rejected the API key. Check it in assistant settings.': 'Anthropic 拒绝了此 API 密钥。请在助手设置中检查。',
  'Rate limited by Anthropic. Wait a moment and try again.': '已被 Anthropic 限流。请稍候再试。',
  'Couldn’t reach {{url}}. Is it running, and does it allow requests from this page (CORS)?': '无法连接 {{url}}。它是否正在运行，并允许来自此页面的请求（CORS）？',
  'For LM Studio: `lms server start --cors`.': 'LM Studio：`lms server start --cors`。',
  'For Ollama: set OLLAMA_ORIGINS.': 'Ollama：设置 OLLAMA_ORIGINS。',
  'Anthropic API error {{status}}: {{message}}': 'Anthropic API 错误 {{status}}：{{message}}',
  'retrying: the model produced malformed tool input': '正在重试：模型生成了格式错误的工具输入',
  'The model declined this request ({{category}}).': '模型拒绝了此请求（{{category}}）。',
  'The model declined this request.': '模型拒绝了此请求。',
  'The model declined to summarise the conversation.': '模型拒绝总结这段对话。',
  'The response was cut off mid tool call (max_tokens).': '响应在工具调用中途被截断（max_tokens）。',
  '{{provider}} returned HTTP {{status}}: {{body}}': '{{provider}} 返回 HTTP {{status}}：{{body}}',
  'The response was cut off mid tool call.': '响应在工具调用中途被截断。',
}

export default zh
