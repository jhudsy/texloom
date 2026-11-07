import type { Catalog } from '../../i18n'
const fr: Catalog = {
  '{{end}} has no matching {{begin}}.': '{{end}} n’a pas de {{begin}} correspondant.',
  '{{begin}} isn’t closed before {{end}}.': '{{begin}} n’est pas fermé avant {{end}}.',
  '{{begin}} isn’t closed in this file.': '{{begin}} n’est pas fermé dans ce fichier.',
  'This } closes nothing: there are more } than { before it.': 'Cette accolade } ne ferme rien : il y a plus de } que de { avant elle.',
  'This { isn’t closed in this file.': 'Cette accolade { n’est pas fermée dans ce fichier.',
  'Put \\label after \\caption: here \\ref gives the number of the section, not of this float.':
    'Placez \\label après \\caption : ici, \\ref donne le numéro de la section, pas celui de ce flottant.',
  "Straight quotes come out as ” on both sides: write ``text'' (two backquotes, then two apostrophes).":
    "Les guillemets droits donnent ” des deux côtés : écrivez ``text'' (deux accents graves, puis deux apostrophes).",
  'Use `` and \'\'': 'Utiliser `` et \'\'',
  'Write \\dots for an ellipsis: three full stops are spaced too tightly.': 'Écrivez \\dots pour des points de suspension : trois points à la suite sont trop serrés.',
  'Use \\dots': 'Utiliser \\dots',
  '“{{word}}” twice in a row.': '« {{word}} » deux fois de suite.',
  'Remove the repeat': 'Supprimer la répétition',
  'LaTeX takes the full stop in “{{abbr}}” as the end of a sentence and adds a wide space: write {{abbr}}\\ (or {{abbr}}~).':
    'LaTeX prend le point de « {{abbr}} » pour une fin de phrase et ajoute une espace large : écrivez {{abbr}}\\ (ou {{abbr}}~).',
  'Use {{code}}': 'Utiliser {{code}}',
  'A non-breaking space (~) keeps {{cmd}} on the same line as the word before it.': 'Une espace insécable (~) garde {{cmd}} sur la même ligne que le mot qui le précède.',
  'Use ~': 'Utiliser ~',
  '$$…$$ is plain TeX: in LaTeX, \\[…\\] spaces displays correctly (and works with fleqn).':
    '$$…$$ relève du plain TeX : en LaTeX, \\[…\\] espace correctement les équations hors texte (et fonctionne avec fleqn).',
  'Use \\[ \\]': 'Utiliser \\[ \\]',
  '{{old}} is from LaTeX 2.09 and doesn’t combine (bold italic): use {{better}} or {{decl}}.':
    '{{old}} date de LaTeX 2.09 et ne se combine pas (gras italique) : utilisez {{better}} ou {{decl}}.',
  'Cite them together ({{cmd}}) for one bracket and sorted numbers.': 'Citez-les ensemble ({{cmd}}) : un seul crochet et des numéros triés.',
  'Combine': 'Regrouper',
  'Write {{cmd}}: as plain letters, “{{name}}” is set in italics like a product of variables.':
    'Écrivez {{cmd}} : en simples lettres, « {{name}} » est composé en italique comme un produit de variables.',
}
export default fr
