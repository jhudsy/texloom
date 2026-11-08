import type { Catalog } from '../../i18n'

const fr: Catalog = {
  'This server needs a creation code to store a list.': 'Ce serveur demande un code de création pour conserver une liste.',
  'The creation code this list was created with has expired.': 'Le code de création utilisé pour cette liste a expiré.',
  'The server has no room for another list.': 'Le serveur n’a plus de place pour une autre liste.',
  'The server refused the list.': 'Le serveur a refusé la liste.',
  'This server keeps project lists only for people with a creation code from its owner.':
    'Ce serveur ne conserve les listes de projets que pour les personnes ayant un code de création fourni par son propriétaire.',
  'This browser or passkey can’t encrypt with a passkey (it needs the “PRF” feature).':
    'Ce navigateur ou cette clé d’accès ne permet pas de chiffrer avec une clé d’accès (il faut la fonction « PRF »).',
  'That passkey doesn’t open a list on this server.': 'Cette clé d’accès n’ouvre aucune liste sur ce serveur.',
  'The projects in this browser will be added to this list, and anyone who has its link can open them. Use it only if the link is yours.':
    'Les projets de ce navigateur seront ajoutés à cette liste, et toute personne ayant son lien pourra les ouvrir. Ne l’utilisez que si ce lien est le vôtre.',
  'Keep this link somewhere safe, such as a password manager: it opens every project on the list.':
    'Gardez ce lien en lieu sûr, par exemple dans un gestionnaire de mots de passe : il ouvre tous les projets de la liste.',
  'Project list': 'Liste de projets',
  'Close': 'Fermer',
  'Cancel': 'Annuler',
  'This browser keeps its list of {{n}} projects on the server, encrypted. The server can’t read it.': [
    'Ce navigateur conserve sa liste de {{n}} projet sur le serveur, chiffrée. Le serveur ne peut pas la lire.',
    'Ce navigateur conserve sa liste de {{n}} projets sur le serveur, chiffrée. Le serveur ne peut pas la lire.',
  ],
  'Sync now': 'Synchroniser',
  'Copied': 'Copié',
  'Copy sync link': 'Copier le lien de synchronisation',
  'Passkey added.': 'Clé d’accès ajoutée.',
  'Add a passkey': 'Ajouter une clé d’accès',
  'Stop on this browser': 'Arrêter sur ce navigateur',
  'Sync link': 'Lien de synchronisation',
  'Keep the list of your projects on the server, encrypted, so it follows you to other computers and browsers. The server can’t read it.':
    'Conservez la liste de vos projets sur le serveur, chiffrée, pour la retrouver sur d’autres ordinateurs et navigateurs. Le serveur ne peut pas la lire.',
  'It is opened by a sync link, and optionally by a passkey. If you lose both, the list is gone; your projects are not, and still open from their own links.':
    'Elle s’ouvre avec un lien de synchronisation et, si vous le souhaitez, une clé d’accès. Si vous perdez les deux, la liste est perdue ; vos projets ne le sont pas et s’ouvrent toujours avec leurs propres liens.',
  'List started. Copy its sync link and keep it safe.': 'Liste créée. Copiez son lien de synchronisation et gardez-le en lieu sûr.',
  'Start a new list': 'Créer une liste',
  'Unlock with a passkey': 'Déverrouiller avec une clé d’accès',
  'Use this link': 'Utiliser ce lien',
  'This server doesn’t keep project lists.': 'Ce serveur ne conserve pas de listes de projets.',
  'That isn’t a sync link.': 'Ce n’est pas un lien de synchronisation.',
  'That list is kept on {{server}}: open the link there.': 'Cette liste est conservée sur {{server}} : ouvrez le lien là-bas.',
  'The TeXLoom app on this computer asked for your project list. Continue only if you just asked for this in the app.':
    'L’application TeXLoom de cet ordinateur a demandé votre liste de projets. Ne continuez que si vous venez de le demander dans l’application.',
}
export default fr
