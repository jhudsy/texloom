import type { Catalog } from '../../i18n'

const fr: Catalog = {
  'This address can’t open the project': 'Cette adresse ne permet pas d’ouvrir le projet',
  'For safety, the address bar doesn’t contain a project’s keys, so a copied address only works in the browser that opened the project.':
    'Par sécurité, la barre d’adresse ne contient pas les clés du projet : une adresse copiée ne fonctionne donc que dans le navigateur qui a ouvert le projet.',
  'Ask whoever sent it to use Share and send you a link. If this is your own project, open it from your project list or from its link.':
    'Demandez à la personne qui vous l’a envoyée d’utiliser Partager et de vous envoyer un lien. S’il s’agit de votre propre projet, ouvrez-le depuis votre liste de projets ou avec son lien.',
  'OK': 'OK',
  'This link names a server that can’t be used: {{server}}': 'Ce lien indique un serveur qui ne peut pas être utilisé : {{server}}',
  "This link opens “{{name}}” on {{server}}, but your project list has it on {{stored}}. A link naming another server could come from someone who runs that server: only open it there if you trust it.\n\nOK: open it on {{stored}}, as before.\nCancel: open it on {{server}}, in this tab only.": "Ce lien ouvre « {{name}} » sur {{server}}, mais votre liste de projets l’a sur {{stored}}. Un lien qui nomme un autre serveur peut venir de quelqu’un qui gère ce serveur : ne l’ouvrez là-bas que si vous lui faites confiance.\n\nOK : l’ouvrir sur {{stored}}, comme avant.\nAnnuler : l’ouvrir sur {{server}}, dans cet onglet seulement.",
}
export default fr
