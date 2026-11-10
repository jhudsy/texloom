import type { Catalog } from '../../i18n'

const zh: Catalog = {
  '(untitled)': '（无标题）',
  '{{n}} words': { other: '{{n}} 字' },
  '{{n}} selected': { other: '已选择 {{n}} 项' },
  'Words of text in {{n}} files (from the main file through \\input/\\include). Maths, comments, code and labels aren’t counted.': { other: '{{n}} 个文件中的正文字数（从主文件经 \\input/\\include 统计）。不计公式、注释、代码和标签。' },
  'No sections yet. \\section{…}, \\chapter{…} etc. appear here.': '还没有章节。\\section{…}、\\chapter{…} 等会显示在这里。',
  'Search all files': '搜索所有文件',
  'Replace with': '替换为',
  'Match case': '区分大小写',
  'Whole words': '全词匹配',
  'Regular expression': '正则表达式',
  'Replace all': '全部替换',
  'Invalid regular expression.': '正则表达式无效。',
  '{{n}} matches in {{files}}': { other: '{{files}} 中有 {{n}} 处匹配' },
  '{{n}} files': { other: '{{n}} 个文件' },
  'No matches.': '无匹配项。',
  'Replace this one': '替换此项',
  'Replace {{n}} matches in {{files}}? (Undo with History if needed.)': { other: '替换 {{files}} 中的 {{n}} 处匹配？（如有需要，可通过历史记录撤销。）' },
  "Suggest replacing {{n}} matches in {{files}}? Each becomes a suggestion in the Review tab.": {"other": "建议替换 {{files}} 中的 {{n}} 处匹配？每一处都会成为“审阅”标签页中的一条建议。"},
}

export default zh
