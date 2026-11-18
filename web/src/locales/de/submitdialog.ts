import type { Catalog } from '../../i18n'

const de: Catalog = {
  "Remove comments": "Kommentare entfernen",
  "arXiv publishes the source, comments included. Lines that are only a comment go; a comment after code leaves its %.": "arXiv veröffentlicht den Quelltext samt Kommentaren. Zeilen, die nur ein Kommentar sind, entfallen; ein Kommentar nach Code hinterlässt sein %.",
  "One .tex file": "Eine .tex-Datei",
  "Put the files read with \\input and \\include into the main file, for journals that want a single file. With BibTeX, the bibliography goes in too.": "Die mit \\input und \\include gelesenen Dateien in die Hauptdatei übernehmen, für Zeitschriften, die eine einzige Datei verlangen. Mit BibTeX kommt auch das Literaturverzeichnis hinein.",
  "Download .zip": ".zip herunterladen",
  "Download for arXiv or a journal": "Für arXiv oder eine Zeitschrift herunterladen",
  "A .zip for arXiv or a journal: the files {{main}} uses, with the compiled bibliography (.bbl) instead of the .bib. Build files and unused files are left out.": "Eine .zip für arXiv oder eine Zeitschrift: die Dateien, die {{main}} verwendet, mit dem kompilierten Literaturverzeichnis (.bbl) statt der .bib. Build-Dateien und ungenutzte Dateien bleiben draußen.",
  "{{n}} files go in": ["{{n}} Datei kommt hinein", "{{n}} Dateien kommen hinein"],
  "{{n}} files are left out": ["{{n}} Datei bleibt draußen", "{{n}} Dateien bleiben draußen"],
  "Compiling for the bibliography…": "Literaturverzeichnis wird kompiliert…",
  "The document has errors: check the bibliography in the .zip.": "Das Dokument hat Fehler: Prüfen Sie das Literaturverzeichnis in der .zip.",
  "The bibliography couldn’t be compiled, so the project’s own .bbl went in.": "Das Literaturverzeichnis ließ sich nicht kompilieren, daher kam die .bbl des Projekts hinein.",
  "The bibliography couldn’t be compiled, so the .bib went in instead of a .bbl. arXiv may not build it.": "Das Literaturverzeichnis ließ sich nicht kompilieren, daher kam die .bib statt einer .bbl hinein. arXiv kann es dann womöglich nicht bauen.",
  "Couldn’t make the .zip: {{error}}": "Die .zip konnte nicht erstellt werden: {{error}}",
}

export default de
