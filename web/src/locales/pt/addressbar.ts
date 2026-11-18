import type { Catalog } from '../../i18n'

const pt: Catalog = {
  'This address can’t open the project': 'Este endereço não pode abrir o projeto',
  'For safety, the address bar doesn’t contain a project’s keys, so a copied address only works in the browser that opened the project.': 'Por segurança, a barra de endereços não contém as chaves do projeto, então um endereço copiado só funciona no navegador que abriu o projeto.',
  'Ask whoever sent it to use Share and send you a link. If this is your own project, open it from your project list or from its link.': 'Peça a quem enviou que use Compartilhar e envie um link. Se o projeto for seu, abra-o pela sua lista de projetos ou pelo link dele.',
  'OK': 'OK',
  'This link names a server that can’t be used: {{server}}': 'Este link indica um servidor que não pode ser usado: {{server}}',
  "This link opens “{{name}}” on {{server}}, but your project list has it on {{stored}}. A link naming another server could come from someone who runs that server: only open it there if you trust it.\n\nOK: open it on {{stored}}, as before.\nCancel: open it on {{server}}, in this tab only.": "Este link abre “{{name}}” em {{server}}, mas sua lista de projetos o tem em {{stored}}. Um link que nomeia outro servidor pode vir de alguém que administra esse servidor: só o abra lá se confiar nele.\n\nOK: abrir em {{stored}}, como antes.\nCancelar: abrir em {{server}}, só nesta aba.",
}

export default pt
