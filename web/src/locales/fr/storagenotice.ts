import type { Catalog } from '../../i18n'

const fr: Catalog = {
  'Safari may also delete a site’s data after about 7 days without a visit.':
    'Safari peut aussi supprimer les données d’un site au bout d’environ 7 jours sans visite.',

  // creation code
  'e.g. 3PYW-WTHZ-PRCF': 'p. ex. 3PYW-WTHZ-PRCF',
  'Creation code': 'Code de création',
  'Code accepted. Its storage allowance is {{total}} MB, of which {{used}} MB is used.':
    'Code accepté. Son quota de stockage est de {{total}} Mo, dont {{used}} Mo utilisés.',
  'That code isn’t valid on this server.': 'Ce code n’est pas valide sur ce serveur.',
  'Couldn’t reach the server; try again when online.': 'Impossible de joindre le serveur ; réessayez une fois en ligne.',
  'Code': 'Code',
  'Cancel': 'Annuler',
  'Save code': 'Enregistrer le code',

  // new project
  'I have a code': 'J’ai un code',
  'The server keeps an encrypted copy, so collaborators get your changes even when you’re offline, and the link always restores the project.':
    'Le serveur garde une copie chiffrée : les collaborateurs reçoivent vos modifications même quand vous êtes hors ligne, et le lien permet toujours de restaurer le projet.',
  'Continue without a server copy': 'Continuer sans copie sur le serveur',
  'Changes reach collaborators only while you’re both online. The project lives in your browser (and theirs): clearing browser data deletes your copy.':
    'Les modifications ne parviennent aux collaborateurs que lorsque vous êtes en ligne en même temps. Le projet est stocké dans votre navigateur (et dans le leur) : effacer les données du navigateur supprime votre copie.',

  // risk banner
  'Not saved on the server: storing new projects here needs a creation code.':
    'Non enregistré sur le serveur : stocker de nouveaux projets ici nécessite un code de création.',
  'Not saved on the server: the creation code this project was created with has expired. Renewing it restores storage.':
    'Non enregistré sur le serveur : le code de création de ce projet a expiré. Le renouveler rétablit le stockage.',
  'Not saved on the server: the storage allowance of the creation code this project was created with is used up.':
    'Non enregistré sur le serveur : le quota de stockage du code de création utilisé pour créer ce projet est épuisé.',
  'Not saved on the server: this project has reached the server’s {{mb}} MB limit.':
    'Non enregistré sur le serveur : ce projet a atteint la limite de {{mb}} Mo du serveur.',
  'Not saved on the server: the server is out of space. Changes are kept here and retried every few minutes.':
    'Non enregistré sur le serveur : le serveur n’a plus d’espace. Les modifications sont conservées ici et un nouvel essai a lieu toutes les quelques minutes.',
  'This project isn’t stored on the server (Settings → Server storage).':
    'Ce projet n’est pas stocké sur le serveur (Paramètres → Stockage sur le serveur).',
  'Its copies are in this browser and with collaborators: clearing this browser’s data deletes yours.':
    'Ses copies se trouvent dans ce navigateur et chez les collaborateurs : effacer les données de ce navigateur supprime la vôtre.',
  'Keep a folder copy to be safe.': 'Par précaution, gardez une copie dans un dossier.',
  'Enter code': 'Saisir un code',
  'Folder copy…': 'Copie dans un dossier…',
  'Hide until next time': 'Masquer jusqu’à la prochaine fois',
  'Hide this warning': 'Masquer cet avertissement',
  'This server is private: new projects need a creation code from its owner. You only need it once per browser; opening projects shared with you never needs one.':
    'Ce serveur est privé : les nouveaux projets nécessitent un code de création fourni par son propriétaire. Il suffit de le saisir une fois par navigateur ; ouvrir un projet partagé avec vous n’en demande jamais.',
  'Not syncing: this server is private, and the creation code this project was created with has expired or been removed. Renewing it restores access.':
    'Synchronisation arrêtée : ce serveur est privé, et le code de création de ce projet a expiré ou a été retiré. Le renouveler rétablit l’accès.',
  'Not syncing: this server is private, and this project needs a creation code from its owner.':
    'Synchronisation arrêtée : ce serveur est privé, et ce projet nécessite un code de création fourni par son propriétaire.',
  'Your copy here is safe and you can keep working, but nobody else gets your changes.':
    'Votre copie locale est intacte et vous pouvez continuer à travailler, mais personne d’autre ne reçoit vos modifications.',
  "Create project": "Créer un projet",
  'A creation code lets this server keep an encrypted copy of projects you create, so co-authors can catch up while you’re offline. You don’t need one to open or edit projects shared with you; without one, a new project is sync-only.': 'Un code de création permet à ce serveur de garder une copie chiffrée des projets que vous créez, pour que vos coauteurs puissent rattraper leur retard pendant que vous êtes hors ligne. Vous n’en avez pas besoin pour ouvrir ou modifier les projets partagés avec vous ; sans code, un nouveau projet est synchronisé uniquement entre appareils.',
  'A creation code lets this server keep an encrypted copy of projects you create.': 'Un code de création permet à ce serveur de garder une copie chiffrée des projets que vous créez.',
  'Where to get one': 'Où en obtenir un',
  '(on {{server}})': '(sur {{server}})',
  'Another server’s address': 'Adresse d’un autre serveur',
  'this server': 'ce serveur',
  'you have a creation code here': 'vous avez un code de création ici',
  'Give a server’s address, starting with https://': 'Indiquez l’adresse d’un serveur, commençant par https://',
  'Where should the new project be kept?': 'Où conserver le nouveau projet ?',
  'Its encrypted copy and its collaborators’ changes go through that server. This page syncs it from there, whichever server you choose.': 'Sa copie chiffrée et les modifications des collaborateurs passent par ce serveur. Cette page le synchronise depuis celui-ci, quel que soit le serveur choisi.',
  'Another server…': 'Autre serveur…',
  'Continue': 'Continuer',
  'Not syncing: this server needs a newer version of TeXLoom ({{version}} or later). Reload the page to get it.': 'Pas de synchronisation : ce serveur a besoin d’une version plus récente de TeXLoom ({{version}} ou ultérieure). Rechargez la page pour l’obtenir.',
  'Not syncing: this project’s server needs a newer version of TeXLoom ({{version}} or later) than this server’s web app. Open the project from {{server}} instead.': 'Pas de synchronisation : le serveur de ce projet a besoin d’une version plus récente de TeXLoom ({{version}} ou ultérieure) que l’application web de ce serveur. Ouvrez plutôt le projet depuis {{server}}.',
  'Reload': 'Recharger',
  'Used when you create a project, so the server keeps an encrypted copy of it. It follows your project list to your other devices. To move a project you own onto it, open that project’s Settings → Sync & storage.': 'Utilisé quand vous créez un projet, pour que le serveur en garde une copie chiffrée. Il suit votre liste de projets sur vos autres appareils. Pour y déplacer un projet dont vous êtes propriétaire, ouvrez Paramètres → Synchronisation et stockage de ce projet.',
  'Your creation code': 'Votre code de création',
  'Change code…': 'Changer de code…',
  'Enter code…': 'Saisir un code…',
  'None entered. This server stores new projects without one.': 'Aucun code saisi. Ce serveur stocke les nouveaux projets sans code.',
  'None entered: new projects are sync-only until you enter one.': 'Aucun code saisi : les nouveaux projets sont en synchronisation seule jusqu’à ce que vous en saisissiez un.',
  'Couldn’t reach the server.': 'Impossible de joindre le serveur.',
  'No longer valid on this server.': 'N’est plus valide sur ce serveur.',
  'Unlimited storage.': 'Stockage illimité.',
  '{{used}} of {{total}} used.': '{{used}} utilisés sur {{total}}.',
  'Keep it on the server': 'Le garder sur le serveur',
  'Stop storing this project on the server?': 'Ne plus stocker ce projet sur le serveur ?',
  'The server’s encrypted copy is deleted at once. From then on, changes pass only between people who have the project open at the same time.': 'La copie chiffrée du serveur est supprimée immédiatement. Ensuite, les modifications ne passent qu’entre les personnes qui ont le projet ouvert en même temps.',
  'Someone who has the link but has never opened the project gets nothing until someone with a copy is online at the same time. Opening it on a new device works the same way.': 'Quelqu’un qui a le lien mais n’a jamais ouvert le projet ne reçoit rien tant que personne ayant une copie n’est en ligne en même temps. Il en va de même pour l’ouvrir sur un nouvel appareil.',
  'A copy kept only in a browser is lost if that browser’s data is cleared. A folder on your computer, or the TeXLoom app, keeps one on disk.': 'Une copie gardée seulement dans un navigateur est perdue si les données de ce navigateur sont effacées. Un dossier sur votre ordinateur, ou l’application TeXLoom, en garde une sur le disque.',
  'A collaborator whose copy is behind stays behind until they’re online together with someone who has the newer changes.': 'Un collaborateur dont la copie est en retard le reste tant qu’il n’est pas en ligne en même temps que quelqu’un qui a les modifications plus récentes.',
  'The server’s backups, if it keeps any, may hold the encrypted copy for a while.': 'Les sauvegardes du serveur, s’il en fait, peuvent garder la copie chiffrée quelque temps.',
  'Nobody else has the project open right now.': 'Personne d’autre n’a le projet ouvert en ce moment.',
  'Delete the server’s copy': 'Supprimer la copie du serveur',
  '{{n}} other people have the project open right now.': ['{{n}} autre personne a le projet ouvert en ce moment.', '{{n}} autres personnes ont le projet ouvert en ce moment.'],
}

export default fr
