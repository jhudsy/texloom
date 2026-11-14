import type { Catalog } from '../../i18n'

const es: Catalog = {
  'Anthropic rejected the API key. Check it in assistant settings.': 'Anthropic ha rechazado la clave de API. Compruébala en los ajustes del asistente.',
  'Rate limited by Anthropic. Wait a moment and try again.': 'Anthropic ha limitado las solicitudes. Espera un momento y vuelve a intentarlo.',
  'Couldn’t reach {{url}}. Is it running, and does it allow requests from this page (CORS)?': 'No se pudo conectar con {{url}}. ¿Está en marcha y permite solicitudes desde esta página (CORS)?',
  'For LM Studio: `lms server start --cors`.': 'Para LM Studio: `lms server start --cors`.',
  'For Ollama: set OLLAMA_ORIGINS.': 'Para Ollama: define OLLAMA_ORIGINS.',
  'Anthropic API error {{status}}: {{message}}': 'Error de la API de Anthropic {{status}}: {{message}}',
  'retrying: the model produced malformed tool input': 'reintentando: el modelo generó datos de herramienta no válidos',
  'The model declined this request ({{category}}).': 'El modelo ha rechazado esta solicitud ({{category}}).',
  'The model declined this request.': 'El modelo ha rechazado esta solicitud.',
  'The model declined to summarise the conversation.': 'El modelo se negó a resumir la conversación.',
  'The response was cut off mid tool call (max_tokens).': 'La respuesta se cortó a mitad de una llamada a herramienta (max_tokens).',
  '{{provider}} returned HTTP {{status}}: {{body}}': '{{provider}} devolvió HTTP {{status}}: {{body}}',
  'The response was cut off mid tool call.': 'La respuesta se cortó a mitad de una llamada a herramienta.',
}

export default es
