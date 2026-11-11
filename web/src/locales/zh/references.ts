import type { Catalog } from '../../i18n'

const zh: Catalog = {
  'No record found for {{doi}}.': '未找到 {{doi}} 的记录。',
  'doi.org answered {{status}}.': 'doi.org 返回 {{status}}。',
  'doi.org didn’t return BibTeX for this id.': 'doi.org 没有返回此编号的 BibTeX。',
  'The record is unexpectedly large.': '记录大小异常。',
  'That doesn’t look like a DOI (10.xxxx/…) or an arXiv id (e.g. 1706.03762).': '这看起来不像 DOI（10.xxxx/…）或 arXiv 编号（例如 1706.03762）。',
  'Couldn’t read the BibTeX that came back.': '无法读取返回的 BibTeX。',
  'Add a reference': '添加参考文献',
  'Close': '关闭',
  '{{file}} (new file)': '{{file}}（新文件）',
  'Bibliography file': '参考文献文件',
  'Add to': '添加到',
  'Creates {{file}} for the entry.': '将为此条目创建 {{file}}。',
  'Adds to {{file}}.': '将添加到 {{file}}。',
  'Also add {{line}} and {{print}} to {{main}}, so the list is printed': '同时将 {{line}} 和 {{print}} 添加到 {{main}}，以便打印文献列表',
  'Also add {{line}} to {{main}}, so the list is printed': '同时将 {{line}} 添加到 {{main}}，以便打印文献列表',
  'Fetching…': '正在获取…',
  'Already in {{file}} as “{{key}}”; cited it.': '已存在于 {{file}} 中，键为“{{key}}”；已引用。',
  'Added to {{file}} as “{{key}}”.': '已添加到 {{file}}，键为“{{key}}”。',
  'Added {{line}} to {{main}}.': '已将 {{line}} 添加到 {{main}}。',
  'Add {{line}} to your document to print it.': '将 {{line}} 添加到文档中即可打印。',
  "Suggested adding it to {{file}} as “{{key}}”.": "已建议将其以“{{key}}”添加到 {{file}}。",
  "Title and authors, a DOI, or an arXiv id": "标题和作者、DOI 或 arXiv 编号",
  "Type words from the title and authors to search Crossref and arXiv, or paste a DOI, a doi.org or arXiv link, or an arXiv id. The entry is fetched from doi.org and added to your .bib file.": "输入标题和作者中的词语以在 Crossref 和 arXiv 中搜索，或粘贴 DOI、doi.org 或 arXiv 链接、arXiv 编号。条目将从 doi.org 获取并添加到你的 .bib 文件中。",
  "Search or add": "搜索或添加",
  "Couldn’t reach {{site}} (offline?).": "无法连接 {{site}}（是否离线？）。",
  "Searching…": "正在搜索…",
  "Choose the one to add:": "选择要添加的条目：",
  "Nothing found. Try other words, or paste the DOI.": "未找到结果。请换些词语，或粘贴 DOI。",
}

export default zh
