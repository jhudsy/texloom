import type { Catalog } from '../../i18n'

const de: Catalog = {
  '{{end}} has no matching {{begin}}.': '{{end}} hat kein passendes {{begin}}.',
  '{{begin}} isn’t closed before {{end}}.': '{{begin}} wird vor {{end}} nicht geschlossen.',
  '{{begin}} isn’t closed in this file.': '{{begin}} wird in dieser Datei nicht geschlossen.',
  'This } closes nothing: there are more } than { before it.': 'Diese } schließt nichts: Davor stehen mehr } als {.',
  'This { isn’t closed in this file.': 'Diese { wird in dieser Datei nicht geschlossen.',
  'Put \\label after \\caption: here \\ref gives the number of the section, not of this float.': 'Setzen Sie \\label hinter \\caption: Hier liefert \\ref die Nummer des Abschnitts, nicht die dieses Gleitobjekts.',
  'Straight quotes come out as ” on both sides: write ``text\'\' (two backquotes, then two apostrophes).': 'Gerade Anführungszeichen erscheinen auf beiden Seiten als ”: Schreiben Sie ``Text\'\' (zwei Backticks, dann zwei Apostrophe).',
  'Use `` and \'\'': '`` und \'\' verwenden',
  'Write \\dots for an ellipsis: three full stops are spaced too tightly.': 'Schreiben Sie \\dots für Auslassungspunkte: Drei Punkte stehen zu eng.',
  'Use \\dots': '\\dots verwenden',
  '“{{word}}” twice in a row.': '„{{word}}“ zweimal hintereinander.',
  'Remove the repeat': 'Wiederholung entfernen',
  'LaTeX takes the full stop in “{{abbr}}” as the end of a sentence and adds a wide space: write {{abbr}}\\ (or {{abbr}}~).': 'LaTeX hält den Punkt in „{{abbr}}“ für das Satzende und fügt einen breiten Abstand ein: Schreiben Sie {{abbr}}\\ (oder {{abbr}}~).',
  'Use {{code}}': '{{code}} verwenden',
  'A non-breaking space (~) keeps {{cmd}} on the same line as the word before it.': 'Ein geschütztes Leerzeichen (~) hält {{cmd}} in derselben Zeile wie das Wort davor.',
  'Use ~': '~ verwenden',
  '$$…$$ is plain TeX: in LaTeX, \\[…\\] spaces displays correctly (and works with fleqn).': '$$…$$ ist Plain TeX: In LaTeX setzt \\[…\\] abgesetzte Formeln mit korrekten Abständen (und funktioniert mit fleqn).',
  'Use \\[ \\]': '\\[ \\] verwenden',
  '{{old}} is from LaTeX 2.09 and doesn’t combine (bold italic): use {{better}} or {{decl}}.': '{{old}} stammt aus LaTeX 2.09 und lässt sich nicht kombinieren (fett kursiv): Verwenden Sie {{better}} oder {{decl}}.',
  'Cite them together ({{cmd}}) for one bracket and sorted numbers.': 'Zitieren Sie sie gemeinsam ({{cmd}}), um eine Klammer und sortierte Nummern zu erhalten.',
  'Combine': 'Zusammenfassen',
  'Write {{cmd}}: as plain letters, “{{name}}” is set in italics like a product of variables.': 'Schreiben Sie {{cmd}}: Als einfache Buchstaben wird „{{name}}“ kursiv wie ein Produkt von Variablen gesetzt.',
}

export default de
