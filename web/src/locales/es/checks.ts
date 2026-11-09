import type { Catalog } from '../../i18n'

const es: Catalog = {
  '{{end}} has no matching {{begin}}.': '{{end}} no tiene su {{begin}} correspondiente.',
  '{{begin}} isn’t closed before {{end}}.': '{{begin}} no se cierra antes de {{end}}.',
  '{{begin}} isn’t closed in this file.': '{{begin}} no se cierra en este archivo.',
  'This } closes nothing: there are more } than { before it.': 'Esta } no cierra nada: hay más } que { antes de ella.',
  'This { isn’t closed in this file.': 'Esta { no se cierra en este archivo.',
  'Put \\label after \\caption: here \\ref gives the number of the section, not of this float.': 'Pon \\label después de \\caption: aquí \\ref da el número de la sección, no el de este flotante.',
  'Straight quotes come out as ” on both sides: write ``text\'\' (two backquotes, then two apostrophes).': 'Las comillas rectas salen como ” a ambos lados: escribe ``texto\'\' (dos acentos graves y luego dos apóstrofos).',
  'Use `` and \'\'': 'Usar `` y \'\'',
  'Write \\dots for an ellipsis: three full stops are spaced too tightly.': 'Escribe \\dots para los puntos suspensivos: tres puntos seguidos quedan demasiado juntos.',
  'Use \\dots': 'Usar \\dots',
  '“{{word}}” twice in a row.': '«{{word}}» dos veces seguidas.',
  'Remove the repeat': 'Quitar la repetición',
  'LaTeX takes the full stop in “{{abbr}}” as the end of a sentence and adds a wide space: write {{abbr}}\\ (or {{abbr}}~).': 'LaTeX toma el punto de «{{abbr}}» como fin de frase y añade un espacio ancho: escribe {{abbr}}\\ (o {{abbr}}~).',
  'Use {{code}}': 'Usar {{code}}',
  'A non-breaking space (~) keeps {{cmd}} on the same line as the word before it.': 'Un espacio de no separación (~) mantiene {{cmd}} en la misma línea que la palabra anterior.',
  'Use ~': 'Usar ~',
  '$$…$$ is plain TeX: in LaTeX, \\[…\\] spaces displays correctly (and works with fleqn).': '$$…$$ es TeX puro: en LaTeX, \\[…\\] espacia bien las fórmulas (y funciona con fleqn).',
  'Use \\[ \\]': 'Usar \\[ \\]',
  '{{old}} is from LaTeX 2.09 and doesn’t combine (bold italic): use {{better}} or {{decl}}.': '{{old}} viene de LaTeX 2.09 y no se combina (negrita cursiva): usa {{better}} o {{decl}}.',
  'Cite them together ({{cmd}}) for one bracket and sorted numbers.': 'Cítalas juntas ({{cmd}}) para tener un solo corchete y los números ordenados.',
  'Combine': 'Combinar',
  'Write {{cmd}}: as plain letters, “{{name}}” is set in italics like a product of variables.': 'Escribe {{cmd}}: como letras sueltas, «{{name}}» se compone en cursiva como un producto de variables.',
}

export default es
