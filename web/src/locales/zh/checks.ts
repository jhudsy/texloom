import type { Catalog } from '../../i18n'

const zh: Catalog = {
  '{{end}} has no matching {{begin}}.': '{{end}} 没有对应的 {{begin}}。',
  '{{begin}} isn’t closed before {{end}}.': '{{begin}} 在 {{end}} 之前未闭合。',
  '{{begin}} isn’t closed in this file.': '{{begin}} 在此文件中未闭合。',
  'This } closes nothing: there are more } than { before it.': '这个 } 没有对应的开括号：它之前的 } 比 { 多。',
  'This { isn’t closed in this file.': '这个 { 在此文件中未闭合。',
  'Put \\label after \\caption: here \\ref gives the number of the section, not of this float.': '请把 \\label 放在 \\caption 之后：在这里 \\ref 得到的是章节编号，而不是此浮动体的编号。',
  'Straight quotes come out as ” on both sides: write ``text\'\' (two backquotes, then two apostrophes).': '直引号两侧都会显示为 ”：请写成 ``文字\'\'（两个反引号，再加两个单引号）。',
  'Use `` and \'\'': '使用 `` 和 \'\'',
  'Write \\dots for an ellipsis: three full stops are spaced too tightly.': '省略号请写 \\dots：三个句点的间距过窄。',
  'Use \\dots': '使用 \\dots',
  '“{{word}}” twice in a row.': '“{{word}}”连续出现了两次。',
  'Remove the repeat': '删除重复',
  'LaTeX takes the full stop in “{{abbr}}” as the end of a sentence and adds a wide space: write {{abbr}}\\ (or {{abbr}}~).': 'LaTeX 会把“{{abbr}}”中的句点当作句末并加宽间距：请写成 {{abbr}}\\ （或 {{abbr}}~）。',
  'Use {{code}}': '使用 {{code}}',
  'A non-breaking space (~) keeps {{cmd}} on the same line as the word before it.': '不换行空格（~）可让 {{cmd}} 与前一个词保持在同一行。',
  'Use ~': '使用 ~',
  '$$…$$ is plain TeX: in LaTeX, \\[…\\] spaces displays correctly (and works with fleqn).': '$$…$$ 是纯 TeX 写法：在 LaTeX 中，\\[…\\] 能正确处理公式间距（并兼容 fleqn）。',
  'Use \\[ \\]': '使用 \\[ \\]',
  '{{old}} is from LaTeX 2.09 and doesn’t combine (bold italic): use {{better}} or {{decl}}.': '{{old}} 来自 LaTeX 2.09，无法组合（如粗斜体）：请使用 {{better}} 或 {{decl}}。',
  'Cite them together ({{cmd}}) for one bracket and sorted numbers.': '将它们合并引用（{{cmd}}），可得到一个方括号和排好序的编号。',
  'Combine': '合并',
  'Write {{cmd}}: as plain letters, “{{name}}” is set in italics like a product of variables.': '请写 {{cmd}}：作为普通字母，“{{name}}”会像变量乘积一样排成斜体。',
}

export default zh
