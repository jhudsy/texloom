import type { Catalog } from '../../i18n'

const pt: Catalog = {
  'Anthropic rejected the API key. Check it in assistant settings.': 'A Anthropic rejeitou a chave de API. Verifique-a nas configurações do assistente.',
  'Rate limited by Anthropic. Wait a moment and try again.': 'Limite de solicitações da Anthropic atingido. Aguarde um momento e tente novamente.',
  'Couldn’t reach {{url}}. Is it running, and does it allow requests from this page (CORS)?': 'Não foi possível acessar {{url}}. Ele está em execução e permite solicitações desta página (CORS)?',
  'For LM Studio: `lms server start --cors`.': 'Para o LM Studio: `lms server start --cors`.',
  'For Ollama: set OLLAMA_ORIGINS.': 'Para o Ollama: defina OLLAMA_ORIGINS.',
  'Anthropic API error {{status}}: {{message}}': 'Erro da API da Anthropic {{status}}: {{message}}',
  'retrying: the model produced malformed tool input': 'tentando novamente: o modelo gerou uma entrada de ferramenta inválida',
  'The model declined this request ({{category}}).': 'O modelo recusou esta solicitação ({{category}}).',
  'The model declined this request.': 'O modelo recusou esta solicitação.',
  'The response was cut off mid tool call (max_tokens).': 'A resposta foi interrompida no meio de uma chamada de ferramenta (max_tokens).',
  '{{provider}} returned HTTP {{status}}: {{body}}': '{{provider}} retornou HTTP {{status}}: {{body}}',
  'The response was cut off mid tool call.': 'A resposta foi interrompida no meio de uma chamada de ferramenta.',
}

export default pt
