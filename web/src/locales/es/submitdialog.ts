import type { Catalog } from '../../i18n'

const es: Catalog = {
  "Remove comments": "Quitar los comentarios",
  "arXiv publishes the source, comments included. Lines that are only a comment go; a comment after code leaves its %.": "arXiv publica el código fuente, comentarios incluidos. Las líneas que solo son un comentario se quitan; un comentario tras el código deja su %.",
  "One .tex file": "Un solo .tex",
  "Put the files read with \\input and \\include into the main file, for journals that want a single file. With BibTeX, the bibliography goes in too.": "Pone los archivos leídos con \\input e \\include dentro del archivo principal, para las revistas que piden un único archivo. Con BibTeX, también entra la bibliografía.",
  "Download .zip": "Descargar .zip",
  "Download for submission": "Descargar para enviar",
  "A .zip for arXiv or a journal: the files {{main}} uses, with the compiled bibliography (.bbl) instead of the .bib. Build files and unused files are left out.": "Un .zip para arXiv o una revista: los archivos que usa {{main}}, con la bibliografía compilada (.bbl) en lugar del .bib. Se dejan fuera los archivos de compilación y los que no se usan.",
  "{{n}} files go in": ["Entra {{n}} archivo", "Entran {{n}} archivos"],
  "{{n}} files are left out": ["Se deja fuera {{n}} archivo", "Se dejan fuera {{n}} archivos"],
  "Compiling for the bibliography…": "Compilando la bibliografía…",
  "The document has errors: check the bibliography in the .zip.": "El documento tiene errores: revise la bibliografía del .zip.",
  "The bibliography couldn’t be compiled, so the project’s own .bbl went in.": "No se pudo compilar la bibliografía, así que se incluyó el .bbl del proyecto.",
  "The bibliography couldn’t be compiled, so the .bib went in instead of a .bbl. arXiv may not build it.": "No se pudo compilar la bibliografía, así que se incluyó el .bib en lugar de un .bbl. Puede que arXiv no lo compile.",
  "Couldn’t make the .zip: {{error}}": "No se pudo crear el .zip: {{error}}",
}

export default es
