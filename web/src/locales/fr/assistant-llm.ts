import type { Catalog } from '../../i18n'
const fr: Catalog = {
  'Anthropic rejected the API key. Check it in assistant settings.': 'Anthropic a refusé la clé d’API. Vérifiez-la dans les paramètres de l’Assistant.',
  'Rate limited by Anthropic. Wait a moment and try again.': 'Limite de débit atteinte chez Anthropic. Patientez un instant, puis réessayez.',
  'Couldn’t reach {{url}}. Is it running, and does it allow requests from this page (CORS)?':
    'Impossible de joindre {{url}}. Le serveur est-il lancé, et accepte-t-il les requêtes de cette page (CORS) ?',
  'For LM Studio: `lms server start --cors`.': 'Pour LM Studio : `lms server start --cors`.',
  'For Ollama: set OLLAMA_ORIGINS.': 'Pour Ollama : définissez OLLAMA_ORIGINS.',
  'Anthropic API error {{status}}: {{message}}': 'Erreur {{status}} de l’API Anthropic : {{message}}',
  'retrying: the model produced malformed tool input': 'nouvelle tentative : le modèle d’IA a produit des arguments d’outil mal formés',
  'The model declined this request ({{category}}).': 'Le modèle d’IA a refusé cette demande ({{category}}).',
  'The model declined this request.': 'Le modèle d’IA a refusé cette demande.',
  'The response was cut off mid tool call (max_tokens).': 'La réponse a été coupée au milieu d’un appel d’outil (max_tokens).',
  '{{provider}} returned HTTP {{status}}: {{body}}': '{{provider}} a répondu HTTP {{status}} : {{body}}',
  'The response was cut off mid tool call.': 'La réponse a été coupée au milieu d’un appel d’outil.',
}
export default fr
