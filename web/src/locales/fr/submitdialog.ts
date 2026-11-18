import type { Catalog } from '../../i18n'

const fr: Catalog = {
  "Remove comments": "Retirer les commentaires",
  "arXiv publishes the source, comments included. Lines that are only a comment go; a comment after code leaves its %.": "arXiv publie la source, commentaires compris. Les lignes qui ne sont qu’un commentaire disparaissent ; un commentaire après du code laisse son %.",
  "One .tex file": "Un seul .tex",
  "Put the files read with \\input and \\include into the main file, for journals that want a single file. With BibTeX, the bibliography goes in too.": "Place les fichiers lus par \\input et \\include dans le fichier principal, pour les revues qui veulent un seul fichier. Avec BibTeX, la bibliographie y entre aussi.",
  "Download .zip": "Télécharger le .zip",
  "Download for arXiv or a journal": "Télécharger pour arXiv ou une revue",
  "A .zip for arXiv or a journal: the files {{main}} uses, with the compiled bibliography (.bbl) instead of the .bib. Build files and unused files are left out.": "Un .zip pour arXiv ou une revue : les fichiers qu’utilise {{main}}, avec la bibliographie compilée (.bbl) au lieu du .bib. Les fichiers de compilation et les fichiers inutilisés sont laissés de côté.",
  "{{n}} files go in": ["{{n}} fichier inclus", "{{n}} fichiers inclus"],
  "{{n}} files are left out": ["{{n}} fichier laissé de côté", "{{n}} fichiers laissés de côté"],
  "Compiling for the bibliography…": "Compilation de la bibliographie…",
  "The document has errors: check the bibliography in the .zip.": "Le document contient des erreurs : vérifiez la bibliographie dans le .zip.",
  "The bibliography couldn’t be compiled, so the project’s own .bbl went in.": "La bibliographie n’a pas pu être compilée ; le .bbl du projet a donc été inclus.",
  "The bibliography couldn’t be compiled, so the .bib went in instead of a .bbl. arXiv may not build it.": "La bibliographie n’a pas pu être compilée ; le .bib a donc été inclus au lieu d’un .bbl. arXiv risque de ne pas pouvoir le compiler.",
  "Couldn’t make the .zip: {{error}}": "Impossible de créer le .zip : {{error}}",
}

export default fr
