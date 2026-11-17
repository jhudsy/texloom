import type { Catalog } from '../../i18n'

const zh: Catalog = {
  "Remove comments": "删除注释",
  "arXiv publishes the source, comments included. Lines that are only a comment go; a comment after code leaves its %.": "arXiv 会公开源文件，包括注释。只有注释的行会被删除；代码后的注释只留下 %。",
  "One .tex file": "合并为一个 .tex",
  "Put the files read with \\input and \\include into the main file, for journals that want a single file. With BibTeX, the bibliography goes in too.": "把通过 \\input 和 \\include 读取的文件并入主文件，适用于要求单个文件的期刊。使用 BibTeX 时，参考文献也会并入。",
  "Download .zip": "下载 .zip",
  "Download for submission": "下载投稿文件",
  "A .zip for arXiv or a journal: the files {{main}} uses, with the compiled bibliography (.bbl) instead of the .bib. Build files and unused files are left out.": "适用于 arXiv 或期刊的 .zip：包含 {{main}} 用到的文件，并以编译好的参考文献（.bbl）代替 .bib。编译生成的文件和未使用的文件不包含在内。",
  "{{n}} files go in": { other: "包含 {{n}} 个文件" },
  "{{n}} files are left out": { other: "{{n}} 个文件未包含" },
  "Compiling for the bibliography…": "正在编译参考文献…",
  "The document has errors: check the bibliography in the .zip.": "文档有错误：请检查 .zip 中的参考文献。",
  "The bibliography couldn’t be compiled, so the project’s own .bbl went in.": "参考文献无法编译，因此放入了项目自带的 .bbl。",
  "The bibliography couldn’t be compiled, so the .bib went in instead of a .bbl. arXiv may not build it.": "参考文献无法编译，因此放入了 .bib 而不是 .bbl。arXiv 可能无法编译它。",
  "Couldn’t make the .zip: {{error}}": "无法生成 .zip：{{error}}",
}

export default zh
