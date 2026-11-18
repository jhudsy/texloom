import type { Catalog } from '../../i18n'

const de: Catalog = {
  'This address can’t open the project': 'Diese Adresse kann das Projekt nicht öffnen',
  'For safety, the address bar doesn’t contain a project’s keys, so a copied address only works in the browser that opened the project.': 'Aus Sicherheitsgründen enthält die Adressleiste nicht die Schlüssel eines Projekts. Eine kopierte Adresse funktioniert daher nur in dem Browser, der das Projekt geöffnet hat.',
  'Ask whoever sent it to use Share and send you a link. If this is your own project, open it from your project list or from its link.': 'Bitten Sie die Person, die sie geschickt hat, über „Teilen“ einen Link zu senden. Wenn es Ihr eigenes Projekt ist, öffnen Sie es über Ihre Projektliste oder seinen Link.',
  'OK': 'OK',
  'This link names a server that can’t be used: {{server}}': 'Dieser Link nennt einen Server, der nicht verwendet werden kann: {{server}}',
  "This link opens “{{name}}” on {{server}}, but your project list has it on {{stored}}. A link naming another server could come from someone who runs that server: only open it there if you trust it.\n\nOK: open it on {{stored}}, as before.\nCancel: open it on {{server}}, in this tab only.": "Dieser Link öffnet „{{name}}“ auf {{server}}, aber Ihre Projektliste hat es auf {{stored}}. Ein Link, der einen anderen Server nennt, kann von jemandem stammen, der diesen Server betreibt: Öffnen Sie es dort nur, wenn Sie ihm vertrauen.\n\nOK: auf {{stored}} öffnen, wie bisher.\nAbbrechen: auf {{server}} öffnen, nur in diesem Tab.",
}

export default de
