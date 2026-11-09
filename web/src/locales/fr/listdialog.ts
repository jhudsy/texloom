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
  'Passkey added. Keep the sync link as well: it still opens the list if you lose this passkey.':
    'Clé d’accès ajoutée. Gardez aussi le lien de synchronisation : il ouvre toujours la liste si vous perdez cette clé.',
  'Add a passkey': 'Ajouter une clé d’accès',
  'Stop on this browser': 'Arrêter sur ce navigateur',
  'Sync link': 'Lien de synchronisation',
  'Keep the list of your projects on the server, encrypted, so it follows you to other computers and browsers. The server can’t read it.':
    'Conservez la liste de vos projets sur le serveur, chiffrée, pour la retrouver sur d’autres ordinateurs et navigateurs. Le serveur ne peut pas la lire.',
  'It is opened by a sync link, and optionally by a passkey or a password.':
    'Elle s’ouvre avec un lien de synchronisation et, si vous le souhaitez, une clé d’accès ou un mot de passe.',
  'The passkey didn’t work. Try again.': 'La clé d’accès n’a pas fonctionné. Réessayez.',
  'There is no way to recover a lost list. The server can’t read it, so nobody can reset it for you. If you lose the sync link and every passkey, and forget any password, the list is gone for good. Your projects aren’t deleted, but you’ll need each project’s own link to open it.':
    'Une liste perdue ne peut pas être récupérée. Le serveur ne peut pas la lire, donc personne ne peut la réinitialiser pour vous. Si vous perdez le lien de synchronisation et toutes vos clés d’accès, et oubliez tout mot de passe, la liste est définitivement perdue. Vos projets ne sont pas supprimés, mais il vous faudra le lien de chaque projet pour l’ouvrir.',
  'List started. Copy its sync link and keep it safe.': 'Liste créée. Copiez son lien de synchronisation et gardez-le en lieu sûr.',
  'Start a new list': 'Créer une liste',
  'Start with a passkey': 'Commencer avec une clé d’accès',
  'Start with a password': 'Commencer avec un mot de passe',
  'Sync link only': 'Lien de synchronisation seul',
  'Open a list you already have': 'Ouvrir une liste existante',
  'List started. Copy its sync link and keep it safe: it’s your way back in if you lose this passkey.':
    'Liste créée. Copiez son lien de synchronisation et gardez-le en lieu sûr : il vous permettra de la retrouver si vous perdez cette clé d’accès.',
  'List started. Copy its sync link and keep it safe: it’s your way back in if you forget this password.':
    'Liste créée. Copiez son lien de synchronisation et gardez-le en lieu sûr : il vous permettra de la retrouver si vous oubliez ce mot de passe.',
  'That username and password are already in use. If the list is yours, use “Unlock with a password” instead.':
    'Ce nom d’utilisateur et ce mot de passe sont déjà utilisés. Si la liste est la vôtre, utilisez plutôt « Déverrouiller avec un mot de passe ».',
  'Unlock with a passkey': 'Déverrouiller avec une clé d’accès',
  'Use this link': 'Utiliser ce lien',
  'This server doesn’t keep project lists.': 'Ce serveur ne conserve pas de listes de projets.',
  'That isn’t a sync link.': 'Ce n’est pas un lien de synchronisation.',
  'That list is kept on {{server}}: open the link there.': 'Cette liste est conservée sur {{server}} : ouvrez le lien là-bas.',
  'The TeXLoom app on this computer asked for your project list. Continue only if you just asked for this in the app.':
    'L’application TeXLoom de cet ordinateur a demandé votre liste de projets. Ne continuez que si vous venez de le demander dans l’application.',
  'This list was replaced or deleted. Stop on this browser, then use its new sync link.':
    'Cette liste a été remplacée ou supprimée. Arrêtez-la sur ce navigateur, puis utilisez son nouveau lien de synchronisation.',
  'That username and password are already in use. Choose another password.':
    'Ce nom d’utilisateur et ce mot de passe sont déjà utilisés. Choisissez un autre mot de passe.',
  'That username and password don’t open a list on this server.':
    'Ce nom d’utilisateur et ce mot de passe n’ouvrent aucune liste sur ce serveur.',
  'Use at least {{n}} characters.':
    'Utilisez au moins {{n}} caractères.',
  'The two passwords don’t match.':
    'Les deux mots de passe ne correspondent pas.',
  'Set a password':
    'Définir un mot de passe',
  'Unlock with a password':
    'Déverrouiller avec un mot de passe',
  'Your username and password stay on this device. Only a value worked out from them reaches the server, so it never learns either one.':
    'Votre nom d’utilisateur et votre mot de passe restent sur cet appareil. Seule une valeur calculée à partir d’eux parvient au serveur, qui ne connaît donc ni l’un ni l’autre.',
  'A forgotten password can’t be reset, by anyone. You’d need your sync link or a passkey to set a new one. Someone who got hold of the server’s data could try to guess it, so use a long password that you don’t use anywhere else.':
    'Un mot de passe oublié ne peut être réinitialisé par personne. Il vous faudrait votre lien de synchronisation ou une clé d’accès pour en définir un nouveau. Quelqu’un qui obtiendrait les données du serveur pourrait essayer de le deviner\xa0\\1 choisissez donc un mot de passe long, que vous n’utilisez nulle part ailleurs.',
  'Username or email (not checked, and never stored)':
    'Nom d’utilisateur ou e-mail (ni vérifié ni conservé)',
  'Password':
    'Mot de passe',
  'That password is too easy to guess.': 'Ce mot de passe est trop facile à deviner.',
  'Password again':
    'Confirmer le mot de passe',
  'OK':
    'OK',
  'Passkey, added {{when}}':
    'Clé d’accès, ajoutée le {{when}}',
  'Password, set {{when}}':
    'Mot de passe, défini le {{when}}',
  'Stop this passkey opening the list? You can also delete it from your password manager.':
    'Empêcher cette clé d’accès d’ouvrir la liste\xa0\\1 Vous pouvez aussi la supprimer de votre gestionnaire de mots de passe.',
  'Remove this password?':
    'Supprimer ce mot de passe\xa0\\1',
  'Other ways to open this list':
    'Autres moyens d’ouvrir cette liste',
  'Working…':
    'Calcul en cours…',
  'Password set.':
    'Mot de passe défini.',
  'If the link has got out, or you’re finished with the list':
    'Si le lien a fuité, ou si vous n’avez plus besoin de la liste',
  'Replace the sync link? The old link, and every passkey and password, will stop working. Other devices will need the new link.':
    'Remplacer le lien de synchronisation\xa0\\1 L’ancien lien, ainsi que toutes les clés d’accès et mots de passe, cesseront de fonctionner. Les autres appareils auront besoin du nouveau lien.',
  'Done. Copy the new sync link and keep it safe. Add any passkeys and password again.':
    'C’est fait. Copiez le nouveau lien de synchronisation et gardez-le en lieu sûr. Ajoutez de nouveau vos clés d’accès et votre mot de passe.',
  'Replace the sync link…':
    'Remplacer le lien de synchronisation…',
  'Delete this list from the server, for every device? Your projects aren’t deleted, and this browser keeps its own list.':
    'Supprimer cette liste du serveur, pour tous les appareils\xa0\\1 Vos projets ne sont pas supprimés, et ce navigateur conserve sa propre liste.',
  'Delete this list…':
    'Supprimer cette liste…',
}
export default fr
