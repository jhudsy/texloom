import type { Catalog } from '../../i18n'

const fr: Catalog = {
  'Remove…': 'Supprimer…',
  'Language': 'Langue',
  'Interface language': 'Langue de l’interface',
  'Automatic (the browser’s language)': 'Automatique (langue du navigateur)',
  // folder help
  'A web page can’t keep a folder in sync while it’s closed. The TeXLoom app can, for all your projects, from the menu bar or tray, and starting at login if you like:':
    'Une page web ne peut pas synchroniser un dossier quand elle est fermée. L’application TeXLoom le peut, pour tous vos projets, depuis la barre des menus ou la zone de notification, et même dès l’ouverture de session si vous le souhaitez :',
  'Open in the TeXLoom app': 'Ouvrir dans l’application TeXLoom',
  'Download the app': 'Télécharger l’application',
  'Or with the texloom command-line helper:': 'Ou avec l’utilitaire texloom en ligne de commande :',
  'syncs all your folders; `texloom autostart on` runs it at login':
    'synchronise tous vos dossiers ; `texloom autostart on` le lance à l’ouverture de session',
  'Edit the files with anything (Claude Code, vim, VS Code); changes flow both ways. Install: see the README.':
    'Modifiez les fichiers avec l’outil de votre choix (Claude Code, vim, VS Code) ; les modifications vont dans les deux sens. Installation : voir le README.',
  'already downloaded on this browser': 'déjà téléchargé dans ce navigateur',
  'one-time download of ≈125 MB, shared by all projects': 'téléchargement unique d’environ 125 Mo, commun à tous les projets',

  // setup dialog
  'Shown to collaborators': 'Visible par les collaborateurs',
  'Open “{{name}}”': 'Ouvrir « {{name}} »',
  'project': 'projet',
  'You can edit this project.': 'Vous pouvez modifier ce projet.',
  'This is a read-only link.': 'Ceci est un lien en lecture seule.',
  'Your name': 'Votre nom',
  'Where do the files live?': 'Où sont stockés les fichiers ?',
  'Only in this browser': 'Uniquement dans ce navigateur',
  'Works offline. Nothing to install. Note: clearing this browser’s data deletes this copy; keep the link so it can be reopened from the server’s copy.':
    'Fonctionne hors ligne. Rien à installer. Attention : effacer les données de ce navigateur supprime cette copie ; conservez le lien pour pouvoir le rouvrir depuis la copie du serveur.',
  'In this browser and in a folder on this computer': 'Dans ce navigateur et dans un dossier sur cet ordinateur',
  'For editing with other tools or agents, and compiling with your own TeX. The folder is a separate copy: delete it like any folder. Choose this only for projects whose editors you trust: your own LaTeX tools may run programs the files ask for.':
    'Pour modifier les fichiers avec d’autres outils ou agents, et compiler avec votre propre TeX. Le dossier est une copie séparée : supprimez-le comme n’importe quel dossier. Ne choisissez cette option que pour des projets dont vous faites confiance aux auteurs : vos propres outils LaTeX peuvent exécuter des programmes demandés par les fichiers.',
  'Compiling': 'Compilation',
  'In this browser': 'Dans ce navigateur',
  'TeX Live 2026 in WebAssembly — {{note}}.': 'TeX Live 2026 en WebAssembly — {{note}}.',
  'With my own TeX installation': 'Avec ma propre installation TeX',
  'Don’t compile here': 'Ne pas compiler ici',
  'Just view the PDFs collaborators publish.': 'Afficher simplement les PDF publiés par les collaborateurs.',
  'Syncing': 'Synchronisation',
  'Live': 'En direct',
  'See collaborators’ changes as they type.': 'Voir les modifications des collaborateurs au fil de leur saisie.',
  'Manual': 'Manuelle',
  'Keep changes on this device until you press Sync.': 'Garder les modifications sur cet appareil jusqu’à ce que vous cliquiez sur Synchroniser.',
  'Start': 'Commencer',

  // settings dialog: server storage
  'needs code': 'code requis',
  'quota full': 'quota épuisé',
  'project full': 'projet plein',
  'server full': 'serveur plein',
  'Keep an encrypted copy on the server': 'Garder une copie chiffrée sur le serveur',
  'Not available yet: this server stores new projects only with a creation code. Until one is entered, your changes are kept here and reach collaborators who are online.':
    'Pas encore disponible : ce serveur ne stocke les nouveaux projets qu’avec un code de création. Tant qu’aucun code n’est saisi, vos modifications sont conservées ici et parviennent aux collaborateurs en ligne.',
  'Collaborators get your changes even if you’re never online at the same time. Right now the server isn’t taking more ({{reason}}): your changes are kept here and sent later.':
    'Les collaborateurs reçoivent vos modifications même si vous n’êtes jamais en ligne en même temps. Pour l’instant, le serveur n’accepte plus de données ({{reason}}) : vos modifications sont conservées ici et seront envoyées plus tard.',
  'Collaborators get your changes even if you’re never online at the same time. The server can’t read it.':
    'Les collaborateurs reçoivent vos modifications même si vous n’êtes jamais en ligne en même temps. Le serveur ne peut pas la lire.',
  'Don’t store anything on the server': 'Ne rien stocker sur le serveur',
  'The server only passes changes between people who are online at the same time. Choosing this deletes the server’s copy.':
    'Le serveur se contente de transmettre les modifications entre les personnes en ligne au même moment. Ce choix supprime la copie du serveur.',
  'Only the project’s owner can delete the server’s copy.': 'Seul le propriétaire du projet peut supprimer la copie du serveur.',
  'Delete the server’s encrypted copy? Changes will only reach collaborators while you are both online.':
    'Supprimer la copie chiffrée du serveur ? Les modifications ne parviendront aux collaborateurs que lorsque vous serez en ligne en même temps.',
  'Cleared': 'Supprimé',
  'Remove downloaded TeX engine': 'Supprimer le moteur TeX téléchargé',

  // settings dialog: project
  'Settings': 'Paramètres',
  'Project': 'Projet',
  'shared with all collaborators': 'commun à tous les collaborateurs',
  'read-only': 'lecture seule',
  'Main file': 'Fichier principal',
  'Engine': 'Moteur',
  'Bibliography': 'Bibliographie',
  'Detect automatically': 'Détection automatique',
  'None': 'Aucune',
  'Spelling': 'Orthographe',
  'Browser’s language': 'Langue du navigateur',
  'English (UK)': 'Anglais (Royaume-Uni)',
  'English (US)': 'Anglais (États-Unis)',
  'German': 'Allemand',
  'French': 'Français',
  'Spanish': 'Espagnol',
  'Italian': 'Italien',
  'Dutch': 'Néerlandais',
  'Portuguese': 'Portugais',
  'Swedish': 'Suédois',
  'Polish': 'Polonais',
  'Server storage': 'Stockage sur le serveur',
  'Enter creation code…': 'Saisir un code de création…',
  'Codes come from whoever runs this server.': 'Les codes sont fournis par la personne qui gère ce serveur.',

  // settings dialog: this device
  'This device': 'Cet appareil',
  'Live sync': 'Synchronisation en direct',
  'Manual sync': 'Synchronisation manuelle',
  'Changes stay here until you press Sync (e.g. to finish a thought, or save bandwidth).':
    'Les modifications restent ici jusqu’à ce que vous cliquiez sur Synchroniser (par exemple pour terminer une idée, ou économiser de la bande passante).',
  'Compile in this browser': 'Compiler dans ce navigateur',
  'Compile with my own TeX': 'Compiler avec mon propre TeX',
  'Recompile automatically': 'Recompiler automatiquement',
  'Recompile automatically after I stop typing': 'Recompiler automatiquement quand j’arrête de taper',
  'Editing': 'Édition',
  'Check spelling': 'Vérifier l’orthographe',
  'Check spelling (uses the browser’s dictionary; LaTeX commands, maths and labels are skipped)':
    'Vérifier l’orthographe (avec le dictionnaire du navigateur ; les commandes LaTeX, les formules et les étiquettes sont ignorées)',
  'Preview equations': 'Aperçu des équations',
  'Preview the equation under the cursor': 'Afficher l’aperçu de l’équation sous le curseur',
  'Point out common mistakes': 'Signaler les erreurs courantes',
  'Point out common LaTeX mistakes (unclosed environments, stray braces, \\label before \\caption, quotes, ties, …)':
    'Signaler les erreurs LaTeX courantes (environnements non fermés, accolades orphelines, \\label avant \\caption, guillemets, espaces insécables, …)',
  'Creation code…': 'Code de création…',
  'Needed only to store new projects on servers that ask for one.':
    'Nécessaire uniquement pour stocker de nouveaux projets sur les serveurs qui en demandent un.',
  'Keep a copy in a folder on this computer': 'Garder une copie dans un dossier sur cet ordinateur',
  'This browser can sync a folder itself while this tab is open: nothing to install. Changes go both ways; files changed on both sides take the folder’s version (it stays in History).':
    'Ce navigateur peut synchroniser lui-même un dossier tant que cet onglet est ouvert : rien à installer. Les modifications vont dans les deux sens ; si un fichier est modifié des deux côtés, c’est la version du dossier qui l’emporte (l’autre reste dans l’Historique).',
  'Collaborators’ files are written there. TeXLoom won’t write files that LaTeX tools run as programs, and points out lines that ask to run one, but compile the folder with your own tools only if you trust everyone who can edit this project.':
    'Les fichiers des collaborateurs y sont écrits. TeXLoom n’écrit pas les fichiers que les outils LaTeX exécutent comme des programmes et signale les lignes qui demandent d’en lancer un, mais ne compilez le dossier avec vos propres outils que si vous faites confiance à toutes les personnes qui peuvent modifier ce projet.',
  'Choose a folder…': 'Choisir un dossier…',
  'Or, to keep it in sync without a browser (and compile with your own TeX):':
    'Ou, pour le synchroniser sans navigateur (et compiler avec votre propre TeX) :',

  // remove
  'Remove': 'Supprimer',
  'code expired': 'code expiré',
  '{{n}} changes from this browser haven’t reached the server or anyone else yet and will be lost.': [
    '{{n}} modification de ce navigateur n’a encore atteint ni le serveur ni personne d’autre et sera perdue.',
    '{{n}} modifications de ce navigateur n’ont encore atteint ni le serveur ni personne d’autre et seront perdues.',
  ],
  'The server keeps no copy of this project: if nobody else has one, it will be gone for good.':
    'Le serveur ne garde aucune copie de ce projet : si personne d’autre n’en a, il sera définitivement perdu.',
  'Remove from this browser': 'Supprimer de ce navigateur',
  'Done': 'Terminé',
  "Suggested changes in the editor:": "Modifications suggérées dans l’éditeur :",
  "Not marked (see the Review tab)": "Non marquées (voir l’onglet Relecture)",
  "Underlined": "Soulignées",
  "In full (removed text struck through)": "En entier (texte supprimé barré)",
  "Remove this project from this browser; or, as the owner, from the server too, or delete it for everyone. Folders on your computer keep their files.": "Retirer ce projet de ce navigateur ; ou, en tant que propriétaire, aussi du serveur, ou le supprimer pour tout le monde. Les dossiers de votre ordinateur gardent leurs fichiers.",
  "The TeXLoom app on this computer keeps this project in sync with a folder. This browser keeps a copy of its own as well.": "L’application TeXLoom de cet ordinateur garde ce projet synchronisé avec un dossier. Ce navigateur en garde aussi sa propre copie.",
  "Your computer compiles it: the TeXLoom app, with “Compile with my own TeX” ticked for this project’s folder (or texloom run <folder> --compile). Its PDF appears here.": "Votre ordinateur le compile : l’application TeXLoom, avec « Compiler avec mon propre TeX » coché pour le dossier de ce projet (ou texloom run <folder> --compile). Son PDF apparaît ici.",
}

export default fr
