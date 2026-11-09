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
    "Language of this app (takes effect when it restarts)": "Langue de l’application (prise en compte au prochain démarrage)",
    "Language: automatic": "Langue : automatique",
    "Language": "Langue",
    "The new language is used the next time TeXLoom starts. Quit TeXLoom (from its menu) and open it again.": "La nouvelle langue sera utilisée au prochain démarrage de TeXLoom. Quittez TeXLoom (depuis son menu) et rouvrez-le.",

    # -- server refusals
    "Change refused": "Modification refusée",
    "The server refused a change from this folder: {reason}": "Le serveur a refusé une modification de ce dossier : {reason}",
    "This server needs a creation code to store new projects: add it to the folder's .texloom/config.json as \"create_code\" and restart the app.": "Ce serveur demande un code de création pour stocker de nouveaux projets : ajoutez-le dans le fichier .texloom/config.json du dossier sous « create_code », puis redémarrez l’application.",
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
    "Share a folder as a new project": "Partager un dossier comme nouveau projet",
    "Folder": "Dossier",
    "Project name": "Nom du projet",
    "Server": "Serveur",
    "only if the server asks for one": "seulement si le serveur en demande un",
    "Creation code": "Code de création",
    "Share": "Partager",
    "Folder to share": "Dossier à partager",
    "Choose an existing folder.": "Choisissez un dossier existant.",
    "The server address starts with https://": "L’adresse du serveur commence par https://",
    "Uploading…": "Envoi en cours…",
    "Couldn’t share it: {err}": "Impossible de le partager : {err}",

    # -- main window
    "Join project…": "Rejoindre un projet…",
    "Share a folder…": "Partager un dossier…",
    "Add existing folder…": "Ajouter un dossier existant…",
    "Pause syncing": "Suspendre la synchronisation",
    "Resume syncing": "Reprendre la synchronisation",
    "Start at login": "Démarrer à l’ouverture de session",
    "Open folder": "Ouvrir le dossier",
    "Open in browser": "Ouvrir dans le navigateur",
    "Links…": "Liens…",
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
    "No folders yet: Join project… or Share a folder…":
        "Aucun dossier pour l’instant : Rejoindre un projet… ou Partager un dossier…",
    "This folder isn't a git repository (run `git init` in it first).":
        "Ce dossier n’est pas un dépôt git (lancez d’abord `git init` dedans).",
    "A texloom folder": "Un dossier texloom",
    "Add folder": "Ajouter un dossier",
    "{d} isn’t a TeXLoom folder. To join a project, use Join project…; to share this folder as a new project, "
    "Share a folder….":
        "{d} n’est pas un dossier TeXLoom. Pour rejoindre un projet, utilisez Rejoindre un projet… ; pour partager "
        "ce dossier comme nouveau projet, Partager un dossier….",
    "That folder is already on the list.": "Ce dossier est déjà dans la liste.",
    "Shared": "Partagé",
    "The folder is now a TeXLoom project. Open it in the browser and use Share there to invite people.":
        "Le dossier est maintenant un projet TeXLoom. Ouvrez-le dans le navigateur et utilisez Partager pour y "
        "inviter des personnes.",
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
    "Keep the list of your projects on the server, encrypted, so it follows you to other computers and browsers. The server can't read it. It is opened by a sync link: keep that link in a password manager, because it opens every project on the list.":
        'Conservez la liste de vos projets sur le serveur, chiffrée, pour la retrouver sur d’autres ordinateurs et navigateurs. Le serveur ne peut pas la lire. Elle s’ouvre avec un lien de synchronisation\xa0: gardez ce lien dans un gestionnaire de mots de passe, car il ouvre tous les projets de la liste.',
    'Sync link':
        'Lien de synchronisation',
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
    'Stop on this computer':
        'Arrêter sur cet ordinateur',
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
    'Join or share a project first, so TeXLoom knows which server to use.':
        'Rejoignez ou partagez d’abord un projet, pour que TeXLoom sache quel serveur utiliser.',
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
    'Create “{name}”': 'Créer « {name} »',
    'A change applies to every project that has the tag.': 'Le changement s’applique à tous les projets qui portent l’étiquette.',
    'New tag': 'Nouvelle étiquette',
    'Add': 'Ajouter',
    'Rename…': 'Renommer…',
    'Delete': 'Supprimer',
    'New name for “{name}”:': 'Nouveau nom pour « {name} » :',
    'A tag called “{name}” already exists. Merge the two?': 'Une étiquette nommée « {name} » existe déjà. Fusionner les deux ?',
    'Search by name or tag…': 'Rechercher par nom ou étiquette…',
    'Sort folders': 'Trier les dossiers',
    'Oldest first': 'Les plus anciens d’abord',
    'Newest first': 'Les plus récents d’abord',
    'Last changed': 'Dernière modification',
    'Name (A to Z)': 'Nom (A à Z)',
    'Name (Z to A)': 'Nom (Z à A)',
    'Filter by tag': 'Filtrer par étiquette',
    'Filter by tag ({n})': 'Filtrer par étiquette ({n})',
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
}
