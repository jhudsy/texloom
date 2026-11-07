import type { Catalog } from '../../i18n'

const fr: Catalog = {
  'Copied': 'Copié',
  'Copy': 'Copier',
  'Opens your own email app with the link filled in': 'Ouvre votre propre messagerie avec le lien déjà inséré',
  'Email…': 'E-mail…',

  // invitation email
  'a LaTeX project': 'un projet LaTeX',
  'Invitation to edit “{{name}}” on TeXLoom': 'Invitation à modifier « {{name}} » sur TeXLoom',
  'Read-only link to “{{name}}” on TeXLoom': 'Lien en lecture seule vers « {{name}} » sur TeXLoom',
  'Hi {{who}},': 'Bonjour {{who}},',
  'Hi,': 'Bonjour,',
  '{{me}} has invited you to edit “{{name}}” on TeXLoom. Open this link in your browser:':
    '{{me}} vous invite à modifier « {{name}} » sur TeXLoom. Ouvrez ce lien dans votre navigateur :',
  'I have invited you to edit “{{name}}” on TeXLoom. Open this link in your browser:':
    'Je vous invite à modifier « {{name}} » sur TeXLoom. Ouvrez ce lien dans votre navigateur :',
  'Here is a read-only link to “{{name}}” on TeXLoom. Open it in your browser:':
    'Voici un lien en lecture seule vers « {{name}} » sur TeXLoom. Ouvrez-le dans votre navigateur :',
  'This link is for you only: please don’t forward it. Anyone who has it can read the project and change it as you.':
    'Ce lien est pour vous seul : merci de ne pas le transférer. Toute personne qui l’a peut lire le projet et le modifier en votre nom.',
  'Anyone who has this link can read the project, so please don’t forward it.':
    'Toute personne qui a ce lien peut lire le projet : merci de ne pas le transférer.',

  // share dialog
  'Name, e.g. Alice': 'Nom, p. ex. Alice',
  'Name for the personal link': 'Nom pour le lien personnel',
  'Start of this link’s key: tells apart people with the same name':
    'Début de la clé de ce lien : permet de distinguer des personnes portant le même nom',
  'revoked {{date}}': 'révoqué le {{date}}',
  'since {{date}}': 'depuis le {{date}}',
  'Copy {{name}}’s edit link': 'Copier le lien de modification de {{name}}',
  'Copy link': 'Copier le lien',
  'Revoke': 'Révoquer',
  'No personal links yet.': 'Aucun lien personnel pour l’instant.',
  'Edit link for {{name}} — send it to them only (also kept in the list below, on this device)':
    'Lien de modification pour {{name}} — à n’envoyer qu’à cette personne (également conservé dans la liste ci-dessous, sur cet appareil)',
  'Revoke {{name}}’s edit link?\n\nThe server will refuse their changes from now on, and so will everyone’s browsers. Edits they already made stay.':
    'Révoquer le lien de modification de {{name}} ?\n\nLe serveur refusera désormais ses modifications, tout comme les navigateurs de chacun. Les modifications déjà faites sont conservées.',
  'You’re offline: connect first so the server can be told.':
    'Vous êtes hors ligne : connectez-vous d’abord pour que le serveur soit prévenu.',
  '{{name}} can no longer edit, but their link still lets them read new changes.\n\nMove the project to new keys so they can’t? Everyone else then needs a new link from you.':
    '{{name}} ne peut plus modifier le projet, mais son lien lui permet toujours de lire les nouvelles modifications.\n\nDéplacer le projet vers de nouvelles clés pour l’en empêcher ? Tous les autres auront alors besoin d’un nouveau lien de votre part.',
  'Your owner link — full access, including creating and revoking personal links. Keep it to yourself.':
    'Votre lien de propriétaire — accès complet, y compris la création et la révocation des liens personnels. Gardez-le pour vous.',
  'Personal edit links': 'Liens de modification personnels',
  'Give each collaborator their own edit link, so you can later revoke one person without affecting the others. Their edits are signed with their own key. For a group, one link named after the group works too (revoking it cuts off the whole group). Links you create are kept on this device, to copy or email again.':
    'Donnez à chaque collaborateur son propre lien de modification : vous pourrez ensuite révoquer une personne sans toucher aux autres. Ses modifications sont signées avec sa propre clé. Pour un groupe, un seul lien au nom du groupe convient aussi (le révoquer coupe l’accès à tout le groupe). Les liens que vous créez sont conservés sur cet appareil, pour pouvoir les copier ou les envoyer à nouveau.',
  'Create link': 'Créer le lien',
  'Your personal edit link (as {{name}}) — don’t share it; ask the owner for links for others':
    'Votre lien de modification personnel (en tant que {{name}}) — ne le partagez pas ; demandez au propriétaire des liens pour les autres',
  'Share this project': 'Partager ce projet',
  'Read-only link': 'Lien en lecture seule',
  '“Email…” opens your own email app. The link travels in that email (not through this server), so anyone who can read the email can open the project.':
    '« E-mail… » ouvre votre propre messagerie. Le lien passe par cet e-mail (et non par ce serveur) : toute personne pouvant lire l’e-mail peut ouvrir le projet.',
  'Keys stay in the part after “#”, which is never sent to the server.':
    'Les clés se trouvent dans la partie après « # », qui n’est jamais envoyée au serveur.',
  'To shut someone out completely, revoke their link and move the project to new keys.':
    'Pour exclure complètement quelqu’un, révoquez son lien et déplacez le projet vers de nouvelles clés.',
  'Move to new keys…': 'Déplacer vers de nouvelles clés…',
  'Copies everything (with history) into a new project with new keys and deletes the server’s copy of this one. Every existing link stops receiving changes; you hand out new ones.':
    'Copie tout (avec l’historique) dans un nouveau projet avec de nouvelles clés et supprime la copie de celui-ci sur le serveur. Les liens existants ne reçoivent plus les modifications ; vous en distribuez de nouveaux.',
  'Move to new keys': 'Déplacer vers de nouvelles clés',
  'Close': 'Fermer',

  // rotating keys
  'Move this project to new keys?\n\nAll current links (yours included) will point to a retired copy. You get a new owner link and create new links for collaborators.':
    'Déplacer ce projet vers de nouvelles clés ?\n\nTous les liens actuels (y compris le vôtre) pointeront vers une copie retirée. Vous obtenez un nouveau lien de propriétaire et créez de nouveaux liens pour les collaborateurs.',
  'Moving to new keys…': 'Déplacement vers de nouvelles clés…',
  'Re-encrypting files. Keep this tab open.': 'Rechiffrement des fichiers. Gardez cet onglet ouvert.',
  'Done. This is the project at its new keys: use Share to send collaborators new links.':
    'Terminé. Voici le projet avec ses nouvelles clés : utilisez Partager pour envoyer de nouveaux liens aux collaborateurs.',
  'Moving failed part-way: {{error}}. The new copy may exist already; reopen this project to check, and try again.':
    'Le déplacement a échoué en cours de route : {{error}}. La nouvelle copie existe peut-être déjà ; rouvrez ce projet pour vérifier, puis réessayez.',
  'Moving failed: {{error}}. Nothing was retired; the project is unchanged.':
    'Le déplacement a échoué : {{error}}. Rien n’a été retiré ; le projet est inchangé.',
}

export default fr
