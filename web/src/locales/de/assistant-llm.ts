import type { Catalog } from '../../i18n'

const de: Catalog = {
  'Anthropic rejected the API key. Check it in assistant settings.': 'Anthropic hat den API-Schlüssel abgelehnt. Prüfen Sie ihn in den Assistent-Einstellungen.',
  'Rate limited by Anthropic. Wait a moment and try again.': 'Anfragelimit von Anthropic erreicht. Warten Sie einen Moment und versuchen Sie es erneut.',
  'Couldn’t reach {{url}}. Is it running, and does it allow requests from this page (CORS)?': '{{url}} ist nicht erreichbar. Läuft der Dienst und erlaubt er Anfragen von dieser Seite (CORS)?',
  'For LM Studio: `lms server start --cors`.': 'Für LM Studio: `lms server start --cors`.',
  'For Ollama: set OLLAMA_ORIGINS.': 'Für Ollama: OLLAMA_ORIGINS setzen.',
  'Anthropic API error {{status}}: {{message}}': 'Anthropic-API-Fehler {{status}}: {{message}}',
  'retrying: the model produced malformed tool input': 'neuer Versuch: Das Modell hat fehlerhafte Werkzeugeingaben erzeugt',
  'The model declined this request ({{category}}).': 'Das Modell hat diese Anfrage abgelehnt ({{category}}).',
  'The model declined this request.': 'Das Modell hat diese Anfrage abgelehnt.',
  'The model declined to summarise the conversation.': 'Das Modell hat es abgelehnt, die Unterhaltung zusammenzufassen.',
  'The response was cut off mid tool call (max_tokens).': 'Die Antwort wurde mitten in einem Werkzeugaufruf abgeschnitten (max_tokens).',
  '{{provider}} returned HTTP {{status}}: {{body}}': '{{provider}} hat HTTP {{status}} zurückgegeben: {{body}}',
  'The response was cut off mid tool call.': 'Die Antwort wurde mitten in einem Werkzeugaufruf abgeschnitten.',
}

export default de
