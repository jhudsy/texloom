import type { Catalog } from '../../i18n'

const pt: Catalog = {
  '{{end}} has no matching {{begin}}.': '{{end}} não tem {{begin}} correspondente.',
  '{{begin}} isn’t closed before {{end}}.': '{{begin}} não foi fechado antes de {{end}}.',
  '{{begin}} isn’t closed in this file.': '{{begin}} não foi fechado neste arquivo.',
  'This } closes nothing: there are more } than { before it.': 'Esta } não fecha nada: há mais } do que { antes dela.',
  'This { isn’t closed in this file.': 'Esta { não foi fechada neste arquivo.',
  'Put \\label after \\caption: here \\ref gives the number of the section, not of this float.': 'Coloque \\label depois de \\caption: aqui \\ref dá o número da seção, não o deste flutuante.',
  'Straight quotes come out as ” on both sides: write ``text\'\' (two backquotes, then two apostrophes).': 'Aspas retas saem como ” dos dois lados: escreva ``texto\'\' (dois acentos graves e depois dois apóstrofos).',
  'Use `` and \'\'': 'Usar `` e \'\'',
  'Write \\dots for an ellipsis: three full stops are spaced too tightly.': 'Escreva \\dots para reticências: três pontos ficam com espaçamento apertado demais.',
  'Use \\dots': 'Usar \\dots',
  '“{{word}}” twice in a row.': '“{{word}}” duas vezes seguidas.',
  'Remove the repeat': 'Remover a repetição',
  'LaTeX takes the full stop in “{{abbr}}” as the end of a sentence and adds a wide space: write {{abbr}}\\ (or {{abbr}}~).': 'O LaTeX entende o ponto de “{{abbr}}” como fim de frase e adiciona um espaço largo: escreva {{abbr}}\\ (ou {{abbr}}~).',
  'Use {{code}}': 'Usar {{code}}',
  'A non-breaking space (~) keeps {{cmd}} on the same line as the word before it.': 'Um espaço inseparável (~) mantém {{cmd}} na mesma linha da palavra anterior.',
  'Use ~': 'Usar ~',
  '$$…$$ is plain TeX: in LaTeX, \\[…\\] spaces displays correctly (and works with fleqn).': '$$…$$ é TeX puro: no LaTeX, \\[…\\] espaça corretamente as equações (e funciona com fleqn).',
  'Use \\[ \\]': 'Usar \\[ \\]',
  '{{old}} is from LaTeX 2.09 and doesn’t combine (bold italic): use {{better}} or {{decl}}.': '{{old}} vem do LaTeX 2.09 e não se combina (negrito itálico): use {{better}} ou {{decl}}.',
  'Cite them together ({{cmd}}) for one bracket and sorted numbers.': 'Cite-as juntas ({{cmd}}) para ter um só colchete e os números em ordem.',
  'Combine': 'Combinar',
  'Write {{cmd}}: as plain letters, “{{name}}” is set in italics like a product of variables.': 'Escreva {{cmd}}: como letras comuns, “{{name}}” sai em itálico, como um produto de variáveis.',
}

export default pt
