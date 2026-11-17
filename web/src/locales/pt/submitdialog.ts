import type { Catalog } from '../../i18n'

const pt: Catalog = {
  "Remove comments": "Remover comentários",
  "arXiv publishes the source, comments included. Lines that are only a comment go; a comment after code leaves its %.": "O arXiv publica a fonte, incluindo os comentários. As linhas que são só um comentário desaparecem; um comentário depois de código deixa o seu %.",
  "One .tex file": "Um só .tex",
  "Put the files read with \\input and \\include into the main file, for journals that want a single file. With BibTeX, the bibliography goes in too.": "Coloca os ficheiros lidos com \\input e \\include no ficheiro principal, para as revistas que pedem um único ficheiro. Com BibTeX, a bibliografia também entra.",
  "Download .zip": "Transferir .zip",
  "Download for submission": "Transferir para submissão",
  "A .zip for arXiv or a journal: the files {{main}} uses, with the compiled bibliography (.bbl) instead of the .bib. Build files and unused files are left out.": "Um .zip para o arXiv ou uma revista: os ficheiros que {{main}} usa, com a bibliografia compilada (.bbl) em vez do .bib. Os ficheiros de compilação e os que não são usados ficam de fora.",
  "{{n}} files go in": ["Entra {{n}} ficheiro", "Entram {{n}} ficheiros"],
  "{{n}} files are left out": ["Fica de fora {{n}} ficheiro", "Ficam de fora {{n}} ficheiros"],
  "Compiling for the bibliography…": "A compilar a bibliografia…",
  "The document has errors: check the bibliography in the .zip.": "O documento tem erros: verifique a bibliografia no .zip.",
  "The bibliography couldn’t be compiled, so the project’s own .bbl went in.": "Não foi possível compilar a bibliografia, por isso entrou o .bbl do próprio projeto.",
  "The bibliography couldn’t be compiled, so the .bib went in instead of a .bbl. arXiv may not build it.": "Não foi possível compilar a bibliografia, por isso entrou o .bib em vez de um .bbl. O arXiv pode não o conseguir compilar.",
  "Couldn’t make the .zip: {{error}}": "Não foi possível criar o .zip: {{error}}",
}

export default pt
