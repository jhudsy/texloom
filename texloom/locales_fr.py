"""French for the desktop app (texloom/app.py), keyed by the exact English strings.

Plural entries are keyed by the English plural form and hold (singular, plural); French
uses the singular for 0 and 1. Spaces before : ; ! ? and inside « » are non-breaking (U+00A0).
`{name}` placeholders must stay as they are.
"""

FR: dict[str, str | tuple[str, str]] = {
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
}
