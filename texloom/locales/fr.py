"""French for the desktop app (texloom/app.py), keyed by the exact English strings.

Plural entries are keyed by the English plural form and hold (singular, plural); French
uses the singular for 0 and 1 (see PLURAL_RULES in texloom/i18n.py). Spaces before : ; ! ? and inside « » are non-breaking (U+00A0).
`{name}` placeholders must stay as they are.
"""

CATALOG: dict[str, str | tuple[str, str] | dict[str, str]] = {
    # -- what a line that runs programs does (texloom/scan.py)
    'runs a shell command (\\write18)': 'exécute une commande shell (\\write18)',
    'runs a shell command (\\ShellEscape)': 'exécute une commande shell (\\ShellEscape)',
    'reads the output of a command (\\input|)': 'lit la sortie d’une commande (\\input|)',
    'runs a program from Lua': 'exécute un programme depuis Lua',
    'loads a package that runs programs': 'charge un paquet qui exécute des programmes',
    'TikZ externalisation runs LaTeX as a command': 'l’externalisation TikZ lance LaTeX comme commande',
    'tells arara which programs to run': 'indique à arara quels programmes exécuter',
    'sets the program your LaTeX editor runs': 'choisit le programme que lance votre éditeur LaTeX',

    # -- compile from scratch
    "Compile from scratch": "Recompiler de zéro",
    "Delete LaTeX’s working files (.aux, .bbl, …) in .texloom/build and compile again": "Supprimer les fichiers de travail de LaTeX (.aux, .bbl, …) dans .texloom/build et recompiler",
    "This folder isn’t running yet: try again in a moment.": "Ce dossier n’est pas encore synchronisé : réessayez dans un instant.",
    "Compiling {name} from scratch…": "Recompilation de {name} depuis zéro…",

    # -- language menu
    "Language": "Langue",
    "The new language is used the next time TeXLoom starts. Quit TeXLoom (from its menu) and open it again.": "La nouvelle langue sera utilisée au prochain démarrage de TeXLoom. Quittez TeXLoom (depuis son menu) et rouvrez-le.",

    # -- server refusals
    "Change refused": "Modification refusée",
    "The server refused a change from this folder: {reason}": "Le serveur a refusé une modification de ce dossier : {reason}",
    "This project's creation code has expired: renewing it restores storage.": "Le code de création de ce projet a expiré\u00a0: le renouveler rétablit le stockage.",
    "The storage allowance of this project's creation code is used up.": "Le quota de stockage du code de création de ce projet est épuisé.",
    "This project has reached the server's size limit.": "Ce projet a atteint la taille maximale autorisée par le serveur.",
    "The server is out of space: changes are kept here and retried.": "Le serveur n’a plus d’espace : les modifications sont conservées ici et renvoyées plus tard.",
    "Not saved on the server": "Non enregistré sur le serveur",
    "This server is private, and this project's creation code has expired or been removed. Renewing it restores access.":
        "Ce serveur est privé, et le code de création de ce projet a expiré ou a été retiré. Le renouveler rétablit l’accès.",
    "This server is private, and this project needs a creation code: add it to the folder's .texloom/config.json as \"create_code\" and restart the app.":
        "Ce serveur est privé, et ce projet nécessite un code de création : ajoutez-le au fichier .texloom/config.json du dossier sous le nom « create_code », puis redémarrez l’application.",
    "Collaborators who are online still get your changes.": "Les collaborateurs en ligne reçoivent quand même vos modifications.",

    # -- folder status
    "Paused": "En pause",
    "Syncing is paused for all folders.": "La synchronisation est en pause pour tous les dossiers.",
    "Starting…": "Démarrage…",
    "Not syncing": "Non synchronisé",
    "Offline": "Hors ligne",
    "Can't reach the server. Changes here are kept and sent when it's back.":
        "Serveur injoignable. Les modifications faites ici sont conservées et seront envoyées à son retour.",
    "Catching up…": "Mise à jour…",
    "Sending {n} changes…": ("Envoi de {n} modification…", "Envoi de {n} modifications…"),
    "Up to date (read-only)": "À jour (lecture seule)",
    "This link can read but not change the project: edits here aren't shared.":
        "Ce lien permet de lire le projet mais pas de le modifier : les modifications faites ici ne sont pas partagées.",
    "Up to date": "À jour",

    # -- lines that run programs
    "Lines that run programs": "Lignes qui lancent des programmes",
    "With “Anything the document asks”, these lines run programs on this computer when it compiles. Only lines "
    "you allow here run; a new or changed line waits for you (that compile runs without it, and the log says so).":
        "Avec « Tout ce que le document demande », ces lignes lancent des programmes sur cet ordinateur lors de la "
        "compilation. Seules les lignes que vous autorisez ici s’exécutent ; une ligne nouvelle ou modifiée attend "
        "votre accord (la compilation se fait sans elle, et le journal l’indique).",
    "Where": "Emplacement",
    "What": "Effet",
    "Line": "Ligne",
    "Anyone with an edit link can change these lines, and TeX macros can hide a command from this list, so allow "
    "this only for projects whose editors you trust.":
        "Toute personne disposant d’un lien de modification peut changer ces lignes, et des macros TeX peuvent "
        "cacher une commande à cette liste : n’autorisez cela que pour des projets dont les éditeurs sont dignes "
        "de confiance.",
    "Allow these lines": "Autoriser ces lignes",
    "Couldn’t read the project: {err}": "Impossible de lire le projet : {err}",
    "new": "nouvelle",
    "allowed": "autorisée",
    "No lines in this project ask to run programs.": "Aucune ligne de ce projet ne demande à lancer un programme.",

    # -- links
    "this folder isn't connected right now: try again in a moment":
        "ce dossier n’est pas connecté pour le moment : réessayez dans un instant",
    "Hi {who},\n\nYou’re invited to edit “{project}” on TeXLoom. Open this link in your browser:\n\n{link}\n\n"
    "This link is for you only: please don’t forward it. Anyone who has it can read the project and change it as you.":
        "Bonjour {who},\n\nVous êtes invité(e) à modifier « {project} » sur TeXLoom. Ouvrez ce lien dans votre "
        "navigateur :\n\n{link}\n\nCe lien vous est personnel : merci de ne pas le transférer. Toute personne qui "
        "le possède peut lire le projet et le modifier en votre nom.",
    "Invitation to edit “{project}” on TeXLoom": "Invitation à modifier « {project} » sur TeXLoom",
    "Links": "Liens",
    "Copy": "Copier",
    "Copied": "Copié",
    "Read-only link": "Lien en lecture seule",
    "Anyone with it can read the project.": "Toute personne qui le possède peut lire le projet.",
    "Your owner link": "Votre lien de propriétaire",
    "Full control, including making and revoking people’s links: keep it to yourself.":
        "Contrôle total, y compris la création et la révocation des liens des autres : gardez-le pour vous.",
    "Edit links for others": "Liens de modification pour d’autres personnes",
    "Each person (or group) gets their own link, so you can later revoke one without affecting the others "
    "(revoking is in the web app: Open in browser → Share).":
        "Chaque personne (ou groupe) reçoit son propre lien, pour pouvoir plus tard en révoquer un sans toucher aux "
        "autres (la révocation se fait dans l’application web : Ouvrir dans le navigateur → Partager).",
    "Name, e.g. Alice, or Lab group": "Nom, p. ex. Alice ou Groupe du labo",
    "Create link": "Créer un lien",
    "Copy link": "Copier le lien",
    "Email…": "E-mail…",
    "Couldn’t create it: {err}": "Impossible de le créer : {err}",
    "Your personal edit link (as {name})": "Votre lien de modification personnel (en tant que {name})",
    "Yours only: don’t pass it on. For others, ask the project’s owner for a link.":
        "Pour vous seul : ne le transmettez pas. Pour d’autres personnes, demandez un lien au propriétaire du projet.",

    # -- joining a project
    "Join a project": "Rejoindre un projet",
    "A web page asked TeXLoom to add this project. Join only if you expected it.":
        "Une page web a demandé à TeXLoom d’ajouter ce projet. Ne le rejoignez que si vous vous y attendiez.",
    "Project link (from the Share dialog, or an email):": "Lien du projet (depuis la fenêtre Partager ou un e-mail) :",
    "Check": "Vérifier",
    "Folder on this computer:": "Dossier sur cet ordinateur :",
    "Choose…": "Choisir…",
    "Compile with my own TeX (latexmk) when files change":
        "Compiler avec mon propre TeX (latexmk) quand les fichiers changent",
    "Collaborators’ files will be written to this folder. TeXLoom won’t write files that LaTeX tools run as "
    "programs, but compile it with your own tools only if you trust everyone who can edit the project.":
        "Les fichiers des collaborateurs seront écrits dans ce dossier. TeXLoom n’écrit pas les fichiers que les "
        "outils LaTeX exécutent comme des programmes, mais ne compilez avec vos propres outils que si vous faites "
        "confiance à tous ceux qui peuvent modifier le projet.",
    "Join": "Rejoindre",
    "Show folder": "Afficher le dossier",
    "Add another copy": "Ajouter une autre copie",
    "Already syncing in {d}.": "Déjà synchronisé dans {d}.",
    "Put the project in…": "Placer le projet dans…",
    "That isn't a TeXLoom project link ({e}).": "Ce n’est pas un lien de projet TeXLoom ({e}).",
    "Looking it up…": "Recherche en cours…",
    "edit access as {member}": "accès en modification en tant que {member}",
    "edit access": "accès en modification",
    "read-only": "lecture seule",
    "Untitled": "Sans titre",
    "<b>“{name}”</b> — {access}, {n} file(s), on {server}":
        ("<b>« {name} »</b> — {access}, {n} fichier, sur {server}",
         "<b>« {name} »</b> — {access}, {n} fichiers, sur {server}"),
    "{d} isn't empty. Choose a new or empty folder.": "{d} n’est pas vide. Choisissez un dossier nouveau ou vide.",
    "Downloading the project…": "Téléchargement du projet…",
    "Couldn’t join: {err}": "Impossible de rejoindre le projet : {err}",

    # -- sharing a folder
    "Folder": "Dossier",
    "Project name": "Nom du projet",
    "Server": "Serveur",
    "only if the server asks for one": "seulement si le serveur en demande un",
    "Creation code": "Code de création",
    "Choose an existing folder.": "Choisissez un dossier existant.",
    "The server address starts with https://": "L’adresse du serveur commence par https://",
    "Uploading…": "Envoi en cours…",

    # -- main window
    "Pause syncing": "Suspendre la synchronisation",
    "Resume syncing": "Reprendre la synchronisation",
    "Open folder": "Ouvrir le dossier",
    "Open in browser": "Ouvrir dans le navigateur",
    "Leave…": "Quitter…",
    "Keep this folder’s git repository in step (commit, pull, push)":
        "Garder le dépôt git de ce dossier synchronisé (commit, pull, push)",
    "Running programs when compiling:": "Lancer des programmes à la compilation :",
    "Never (safest)": "Jamais (le plus sûr)",
    "TeX Live’s safe list only (e.g. epstopdf)": "Liste sûre de TeX Live uniquement (p. ex. epstopdf)",
    "Anything the document asks, once I allow it (e.g. minted)":
        "Tout ce que le document demande, une fois autorisé (p. ex. minted)",
    "Review lines…": "Examiner les lignes…",
    "Activity": "Activité",
    "{n} folders": ("{n} dossier", "{n} dossiers"),
    "{n} not syncing": ("{n} non synchronisé", "{n} non synchronisés"),
    "A texloom folder": "Un dossier texloom",
    "Add folder": "Ajouter un dossier",
    "That folder is already on the list.": "Ce dossier est déjà dans la liste.",
    "Leave": "Quitter",
    "Stop syncing “{project}”?\n\nThe folder and its files stay where they are ({d}); you can delete them yourself.":
        "Arrêter de synchroniser « {project} » ?\n\nLe dossier et ses fichiers restent où ils sont ({d}) ; vous "
        "pouvez les supprimer vous-même.",
    "Running programs": "Lancement de programmes",
    "Compiles may then run programs that the document asks for, on this computer, once you allow each line "
    "(Review lines…).\n\nAnyone with an edit link writes the document. Turn this on only for projects whose "
    "editors you trust.":
        "Les compilations pourront alors lancer sur cet ordinateur les programmes demandés par le document, une "
        "fois chaque ligne autorisée (Examiner les lignes…).\n\nToute personne disposant d’un lien de modification "
        "écrit le document. N’activez cela que pour des projets dont les éditeurs sont dignes de confiance.",
    "Couldn’t change it: {e}": "Impossible de le modifier : {e}",

    # -- tray menu
    "Open TeXLoom": "Ouvrir TeXLoom",
    "Quit TeXLoom": "Quitter TeXLoom",

    # -- project list
    'Project list':
        'Liste de projets',
    'Use this link':
        'Utiliser ce lien',
    'Creation code (if the server needs one)':
        'Code de création (si le serveur en demande un)',
    'Start a new list':
        'Créer une liste',
    'Unlock with a passkey…':
        'Déverrouiller avec une clé d’accès…',
    "Opens the server's page in your browser, which sends the list back to this app.":
        'Ouvre la page du serveur dans votre navigateur, qui renvoie la liste à cette application.',
    'Join…':
        'Rejoindre…',
    'Unlock the list in your browser; it will come back here.':
        'Déverrouillez la liste dans votre navigateur\xa0; elle reviendra ici.',
    'This computer no longer uses a project list. The list itself stays on the server.':
        'Cet ordinateur n’utilise plus de liste de projets. La liste reste sur le serveur.',
    'Close': 'Fermer',
    "There is no way to recover a lost list. The server can't read it, so nobody can reset it for you. If you lose the sync link and every passkey, and forget any password, the list is gone for good. Your projects aren't deleted, but you'll need each project's own link to open it.":
        'Une liste perdue ne peut pas être récupérée. Le serveur ne peut pas la lire, donc personne ne peut la réinitialiser pour vous. Si vous perdez le lien de synchronisation et toutes vos clés d’accès, et oubliez tout mot de passe, la liste est définitivement perdue. Vos projets ne sont pas supprimés, mais il vous faudra le lien de chaque projet pour l’ouvrir.',
    'Unlock with a password…': 'Déverrouiller avec un mot de passe…',
    'This list was replaced or deleted. Use its new sync link.': 'Cette liste a été remplacée ou supprimée. Utilisez son nouveau lien de synchronisation.',
    "That username and password don't open a list on this server.": 'Ce nom d’utilisateur et ce mot de passe n’ouvrent aucune liste sur ce serveur.',
    'Username or email:': 'Nom d’utilisateur ou e-mail\xa0:',
    'Password:': 'Mot de passe\xa0:',
    'Working…': 'Calcul en cours…',
    'Project list…':
        'Liste de projets…',
    'A web page asked TeXLoom to use this project list. Your folders would be added to it, and anyone who has this link could open them. Use it only if the link is yours.':
        'Une page web a demandé à TeXLoom d’utiliser cette liste de projets. Vos dossiers y seraient ajoutés, et toute personne ayant ce lien pourrait les ouvrir. Ne l’utilisez que si ce lien est le vôtre.',
    "Projects on the list that aren't on this computer:":
        'Projets de la liste absents de cet ordinateur\xa0:',
    'This computer has no project list yet.':
        'Cet ordinateur n’a pas encore de liste de projets.',
    'Untitled project':
        'Projet sans titre',
    "That isn't a sync link.":
        'Ce n’est pas un lien de synchronisation.',
    'This server needs a creation code to store a list.':
        'Ce serveur demande un code de création pour conserver une liste.',
    'The creation code this list was created with has expired.':
        'Le code de création utilisé pour cette liste a expiré.',
    'The server has no room for another list.':
        'Le serveur n’a plus de place pour une autre liste.',
    'The list has {n} projects.': ('La liste contient {n} projet.', 'La liste contient {n} projets.'),
    # -- tags, sorting and copying
    'The copy is a new project of your own, with its own links, in a folder of its own. It starts with the current files and settings, without the history, comments or collaborators, and it counts towards your storage.': 'La copie est un nouveau projet à vous, avec ses propres liens, dans son propre dossier. Elle reprend les fichiers et réglages actuels, sans l’historique, les commentaires ni les collaborateurs, et compte dans votre espace de stockage.',
    'Make a copy': 'Faire une copie',
    'Make a copy…': 'Faire une copie…',
    'Copy of {name}': 'Copie de {name}',
    'Choose a folder outside the project being copied.': 'Choisissez un dossier en dehors du projet copié.',
    'Choose a new or empty folder.': 'Choisissez un dossier nouveau ou vide.',
    'Copying…': 'Copie en cours…',
    'Couldn’t copy the project: {err}': 'Impossible de copier le projet : {err}',
    'Only you see your tags. They aren’t shared with collaborators.': 'Vos étiquettes ne sont visibles que par vous. Elles ne sont pas partagées avec les collaborateurs.',
    'A filled box means only some of these projects have that tag.': 'Une case remplie signifie que seuls certains de ces projets portent cette étiquette.',
    'Your tags stay on this computer until you enter a creation code in Project list….': 'Vos étiquettes restent sur cet ordinateur tant que vous n’avez pas saisi de code de création dans Liste de projets….',
    'Find or create a tag…': 'Trouver ou créer une étiquette…',
    'Manage tags…': 'Gérer les étiquettes…',
    'Manage tags': 'Gérer les étiquettes',
    'Codes come from whoever runs this server.': 'Les codes sont fournis par la personne qui gère ce serveur.',
    'Where to get one': 'Où en obtenir un',
    'Connect an AI tool via MCP…': 'Connecter un outil d’IA via MCP…',
    'Connect an AI tool via MCP': 'Connecter un outil d’IA via MCP',
    'If your tool can’t find the texloom command, give its full path instead.': 'Si votre outil ne trouve pas la commande texloom, indiquez son chemin complet.',
    'Suggesting and editing use this folder’s path. Reading uses a read-only link, which your tool saves in its settings, so anyone who can read those settings can read the project.': 'La suggestion et la modification utilisent le chemin de ce dossier. La lecture utilise un lien en lecture seule, que votre outil enregistre dans ses réglages : toute personne pouvant lire ces réglages peut lire le projet.',
    'Tools that support MCP, such as Claude Code, Claude Desktop or Cursor, can work on this project like a collaborator. They run on your computer under your own account with that tool, so no API key is needed here.': 'Les outils compatibles MCP, comme Claude Code, Claude Desktop ou Cursor, peuvent travailler sur ce projet comme un collaborateur. Ils s’exécutent sur votre ordinateur avec votre propre compte, donc aucune clé API n’est nécessaire ici.',
    'Install the TeXLoom command-line helper once': 'Installez une seule fois l’outil TeXLoom en ligne de commande',
    'How to install uv': 'Comment installer uv',
    'Choose what the tool may do': 'Choisissez ce que l’outil peut faire',
    'Suggest changes for people to accept': 'Suggérer des modifications à accepter',
    'Edit files directly': 'Modifier directement les fichiers',
    'Read the project only': 'Lire le projet uniquement',
    'Add it to your tool': 'Ajoutez-le à votre outil',
    'Claude Code: run this in a terminal': 'Claude Code : exécutez ceci dans un terminal',
    'Other tools: add this to their MCP settings': 'Autres outils : ajoutez ceci à leurs réglages MCP',
    'The tool reads text other people wrote in this project, which could include instructions aimed at it. On shared projects, suggesting is the safer choice.': 'L’outil lit le texte écrit par d’autres personnes dans ce projet, qui peut contenir des instructions qui lui sont destinées. Sur les projets partagés, la suggestion est le choix le plus sûr.',
    'Red': 'Rouge',
    'Orange': 'Orange',
    'Yellow': 'Jaune',
    'Green': 'Vert',
    'Teal': 'Turquoise',
    'Blue': 'Bleu',
    'Indigo': 'Indigo',
    'Purple': 'Violet',
    'Pink': 'Rose',
    'Grey': 'Gris',
    'Colour': 'Couleur',
    'Create “{name}”': 'Créer « {name} »',
    'A change applies to every project that has the tag.': 'Le changement s’applique à tous les projets qui portent l’étiquette.',
    'New tag': 'Nouvelle étiquette',
    'Add': 'Ajouter',
    'Rename…': 'Renommer…',
    'Delete': 'Supprimer',
    'New name for “{name}”:': 'Nouveau nom pour « {name} » :',
    'A tag called “{name}” already exists. Merge the two?': 'Une étiquette nommée « {name} » existe déjà. Fusionner les deux ?',
    'Search by name or tag…': 'Rechercher par nom ou étiquette…',
    'Oldest first': 'Les plus anciens d’abord',
    'Newest first': 'Les plus récents d’abord',
    'Last changed': 'Dernière modification',
    'Name (A to Z)': 'Nom (A à Z)',
    'Name (Z to A)': 'Nom (Z à A)',
    'Filter by tag': 'Filtrer par étiquette',
    'No tags yet': 'Aucune étiquette pour le moment',
    'Clear filter': 'Effacer le filtre',
    'Tags…': 'Étiquettes…',
    'Tags for “{name}”': 'Étiquettes de « {name} »',
    '{n} projects': ('{n} projet', '{n} projets'),
    'Delete the tag “{name}”? It will be taken off {n} projects. The projects themselves aren’t changed.': ('Supprimer l’étiquette « {name} » ? Elle sera retirée de {n} projet. Le projet lui-même n’est pas modifié.', 'Supprimer l’étiquette « {name} » ? Elle sera retirée de {n} projets. Les projets eux-mêmes ne sont pas modifiés.'),
    'Tags for {n} folders': ('Étiquettes de {n} dossier', 'Étiquettes de {n} dossiers'),
    '{n} shown': ('{n} affiché', '{n} affichés'),
    '{n} folders selected': ('{n} dossier sélectionné', '{n} dossiers sélectionnés'),
    'Stop syncing {n} folders?\n\n{names}\n\nThe folders and their files stay where they are; you can delete them yourself.': ('Arrêter de synchroniser {n} dossier ?\n\n{names}\n\nLe dossier et ses fichiers restent où ils sont ; vous pouvez le supprimer vous-même.', 'Arrêter de synchroniser {n} dossiers ?\n\n{names}\n\nLes dossiers et leurs fichiers restent où ils sont ; vous pouvez les supprimer vous-même.'),
    'Word count…': 'Nombre de mots…',
    'Count the words with TeXCount, from your own TeX installation': 'Compter les mots avec TeXCount, depuis votre propre installation de TeX',
    'Word count': 'Nombre de mots',
    'TeXCount wasn’t found. It comes with TeX Live and most TeX installations.': 'TeXCount est introuvable. Il est fourni avec TeX Live et la plupart des installations de TeX.',
    'TeXCount couldn’t count this folder: {err}': 'TeXCount n’a pas pu compter ce dossier : {err}',
    'Word count of “{name}”': 'Nombre de mots de « {name} »',
    'No .tex file with \\documentclass found.': 'Aucun fichier .tex avec \\documentclass trouvé.',
    "Deleted by the owner": "Supprimé par le propriétaire",
    "The owner deleted this project for everyone. The files in this folder stay, but nothing syncs any more: use Leave… to take it off the list.": "Le propriétaire a supprimé ce projet pour tout le monde. Les fichiers de ce dossier restent, mais plus rien n’est synchronisé : utilisez Quitter… pour le retirer de la liste.",
    "TeXCount took too long.": "TeXCount a mis trop de temps.",

    # -- the window, menus, settings and welcome page
    'New project from a folder': 'Nouveau projet à partir d’un dossier',
    'The folder’s files are uploaded, encrypted, and the folder stays in step with the project from then on. You can invite people once it’s made.': 'Les fichiers du dossier sont envoyés, chiffrés, et le dossier reste ensuite synchronisé avec le projet. Vous pourrez inviter des personnes une fois le projet créé.',
    'Create project': 'Créer le projet',
    'Folder for the new project': 'Dossier du nouveau projet',
    'Your project list follows you to your other computers and browsers. It is kept on the server, encrypted, and opened by a sync link that only you have.': 'Votre liste de projets vous suit sur vos autres ordinateurs et navigateurs. Elle est conservée sur le serveur, chiffrée, et s’ouvre avec un lien de synchronisation que vous seul possédez.',
    'or paste its sync link': 'ou collez son lien de synchronisation',
    'Copy sync link': 'Copier le lien de synchronisation',
    'Stop using it here': 'Ne plus l’utiliser ici',
    'Settings': 'Réglages',
    'Start TeXLoom when I log in, so folders keep syncing': 'Lancer TeXLoom à l’ouverture de session, pour que les dossiers restent synchronisés',
    'Startup': 'Démarrage',
    'Automatic (the system’s language)': 'Automatique (langue du système)',
    'New project from a folder…': 'Nouveau projet à partir d’un dossier…',
    'Join a project with a link…': 'Rejoindre un projet avec un lien…',
    'Add a TeXLoom folder from this computer…': 'Ajouter un dossier TeXLoom de cet ordinateur…',
    'Settings…': 'Réglages…',
    'Close window': 'Fermer la fenêtre',
    'Share…': 'Partager…',
    'Show activity': 'Afficher l’activité',
    'TeXLoom help': 'Aide de TeXLoom',
    'About TeXLoom': 'À propos de TeXLoom',
    'Add project': 'Ajouter un projet',
    'Make a new project from a folder, or join one with a link': 'Créer un projet à partir d’un dossier, ou en rejoindre un avec un lien',
    'File': 'Fichier',
    'Project': 'Projet',
    'View': 'Présentation',
    'Sort by': 'Trier par',
    'Help': 'Aide',
    'Window': 'Fenêtre',
    'Welcome to TeXLoom': 'Bienvenue dans TeXLoom',
    'TeXLoom keeps a folder on this computer in step with a project that you and your collaborators edit in the browser, so you can also work on it with your own tools.': 'TeXLoom synchronise un dossier de cet ordinateur avec un projet que vous et vos collaborateurs modifiez dans le navigateur, pour que vous puissiez aussi y travailler avec vos propres outils.',
    'More': 'Plus',
    'This folder isn’t a git repository. Run “git init” in it first to use this.': 'Ce dossier n’est pas un dépôt git. Lancez «\xa0git init\xa0» dedans pour utiliser cette option.',
    'Already have a list?': 'Vous avez déjà une liste\xa0?',
    'New here?': 'Première fois\xa0?',
    'Keep the sync link in a password manager: it opens every project on the list, and a lost list can’t be recovered.': 'Gardez le lien de synchronisation dans un gestionnaire de mots de passe\xa0: il ouvre tous les projets de la liste, et une liste perdue ne peut pas être récupérée.',
    'A new language is used the next time TeXLoom starts.': 'Une nouvelle langue est utilisée au prochain démarrage de TeXLoom.',
    'Keeps the list of your projects in step across your computers and browsers.': 'Synchronise la liste de vos projets entre vos ordinateurs et navigateurs.',
    'In use on this computer': 'Utilisée sur cet ordinateur',
    'Not set up': 'Non configurée',
    'Sort and filter': 'Trier et filtrer',
    'Compiling': 'Compilation',
    'Git': 'Git',
    '{d} isn’t a TeXLoom folder yet. To turn it into a project, use Add project ▸ New project from a folder….': '{d} n’est pas encore un dossier TeXLoom. Pour en faire un projet, utilisez Ajouter un projet ▸ Nouveau projet à partir d’un dossier….',
    'The folder is now a TeXLoom project. To invite people, use Share….': 'Le dossier est maintenant un projet TeXLoom. Pour inviter des personnes, utilisez Partager….',
    'Keep syncing?': 'Continuer la synchronisation\xa0?',
    'This desktop has no notification area for TeXLoom to wait in, so closing the window stops syncing. Minimise it instead to keep your folders in step.': 'Ce bureau n’a pas de zone de notification où TeXLoom peut patienter, donc fermer la fenêtre arrête la synchronisation. Réduisez-la plutôt pour garder vos dossiers synchronisés.',
    'Minimise': 'Réduire',
    'Quit': 'Quitter',
    'Don’t ask again': 'Ne plus demander',
    'Couldn’t create the project: {err}': 'Impossible de créer le projet\xa0: {err}',
    'TeXLoom keeps syncing in the background. To quit, use its icon in the notification area.': 'TeXLoom continue la synchronisation en arrière-plan. Pour quitter, utilisez son icône dans la zone de notification.',
    'Keeps folders on this computer in step with TeXLoom projects.': 'Synchronise des dossiers de cet ordinateur avec des projets TeXLoom.',
    'Open your project list…': 'Ouvrir votre liste de projets…',
    'This computer uses a different list now: this one would take its place.': 'Cet ordinateur utilise actuellement une autre liste\xa0: celle-ci la remplacerait.',
    'TeXLoom is free software (AGPL-3.0).': 'TeXLoom est un logiciel libre (AGPL-3.0).',
    'Source code': 'Code source',
    'Licences…': 'Licences…',
    'Licences': 'Licences',
    'Check for new versions once a day': 'Rechercher une nouvelle version une fois par jour',
    'Updates': 'Mises à jour',
    'Check for updates…': 'Rechercher des mises à jour…',
    'Update available': 'Mise à jour disponible',
    'TeXLoom {version} is available. You have {current}.': 'TeXLoom {version} est disponible. Vous avez la version {current}.',
    'What’s new': 'Nouveautés',
    'Update to {version}': 'Mettre à jour vers {version}',
    'Download it, quit TeXLoom (from its menu), and put the new app in place of this one. Your folders and settings stay as they are.': 'Téléchargez-la, quittez TeXLoom (depuis son menu) et remplacez cette application par la nouvelle. Vos dossiers et réglages restent tels quels.',
    'Update it the way you installed it, for example: uv tool upgrade texloom': 'Mettez-le à jour comme vous l’avez installé, par exemple\xa0: uv tool upgrade texloom',
    'Download': 'Télécharger',
    'Later': 'Plus tard',
    'latexmk wasn’t found on this computer, so it can’t compile here. Install TeX Live (MacTeX on a Mac, TeX Live or MiKTeX on Windows), then open TeXLoom again.': 'latexmk est introuvable sur cet ordinateur, la compilation y est donc impossible. Installez TeX Live (MacTeX sur Mac, TeX Live ou MiKTeX sous Windows), puis rouvrez TeXLoom.',
    'TeXLoom {version} is available.': 'TeXLoom {version} est disponible.',
    'Check for updates': 'Rechercher des mises à jour',
    'Couldn’t reach GitHub to check: {err}': 'Impossible de joindre GitHub pour vérifier\xa0: {err}',
    'You have the newest version, {version}.': 'Vous avez la version la plus récente, {version}.',
    'Quit TeXLoom?': 'Quitter TeXLoom\xa0?',
    'Your folders stop syncing until TeXLoom runs again.': 'Vos dossiers ne sont plus synchronisés jusqu’à ce que TeXLoom soit relancé.',
    'It starts again when you next log in.': 'Il redémarre à votre prochaine ouverture de session.',
    'To keep them in sync, let it run in the background: closing the window does that too.': 'Pour qu’ils restent synchronisés, laissez-le tourner en arrière-plan\xa0: fermer la fenêtre suffit.',
    'Keep running in the background': 'Continuer en arrière-plan',
    'Ask before quitting, since folders then stop syncing': 'Demander avant de quitter, car les dossiers ne sont alors plus synchronisés',
    'Up to date (sync-only)': 'À jour (synchronisation seule)',
    "The server keeps no copy of this project: changes and files reach your other computers and collaborators while you're online at the same time.": 'Le serveur ne garde aucune copie de ce projet\xa0: les modifications et les fichiers parviennent à vos autres ordinateurs et à vos collaborateurs tant que vous êtes en ligne en même temps.',
    'Without a code, a server that asks for one keeps no copy: the project syncs only between computers that are online at the same time. You can add a code later.': 'Sans code, un serveur qui en demande un ne garde aucune copie\xa0: le projet se synchronise seulement entre des ordinateurs en ligne en même temps. Vous pourrez ajouter un code plus tard.',
    'Keep a copy on the server…': 'Garder une copie sur le serveur…',
    'This project is sync-only: with a creation code, the server keeps an encrypted copy': 'Ce projet est en synchronisation seule\xa0: avec un code de création, le serveur en garde une copie chiffrée',
    'Keep a copy on the server': 'Garder une copie sur le serveur',
    'Uploading a copy to the server…': 'Envoi d’une copie au serveur…',
    'This server keeps projects only with a creation code, and this one has none. Its owner can add one, or make it sync-only.': 'Ce serveur ne garde les projets qu’avec un code de création, et celui-ci n’en a pas. Son propriétaire peut en ajouter un, ou le passer en synchronisation seule.',
    "The server keeps an encrypted copy of a project only with a creation code. With one, your other computers and collaborators get changes even when you're not online together, and the project opens anywhere in the browser.": 'Le serveur ne garde une copie chiffrée d’un projet qu’avec un code de création. Avec un code, vos autres ordinateurs et vos collaborateurs reçoivent les modifications même si vous n’êtes pas en ligne en même temps, et le projet s’ouvre partout dans le navigateur.',
    "The server didn't accept that code.": 'Le serveur n’a pas accepté ce code.',
    "Couldn't reach the server: {err}": 'Impossible de joindre le serveur\xa0: {err}',
}
