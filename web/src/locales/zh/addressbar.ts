import type { Catalog } from '../../i18n'

const zh: Catalog = {
  'This address can’t open the project': '此地址无法打开该项目',
  'For safety, the address bar doesn’t contain a project’s keys, so a copied address only works in the browser that opened the project.': '出于安全考虑，地址栏中不包含项目密钥，因此复制的地址只能在打开过该项目的浏览器中使用。',
  'Ask whoever sent it to use Share and send you a link. If this is your own project, open it from your project list or from its link.': '请让发送者使用“共享”功能给你发送链接。如果这是你自己的项目，请从项目列表或项目链接打开。',
  'OK': '确定',
  'This link names a server that can’t be used: {{server}}': '此链接指向一个无法使用的服务器：{{server}}',
  "This link opens “{{name}}” on {{server}}, but your project list has it on {{stored}}. A link naming another server could come from someone who runs that server: only open it there if you trust it.\n\nOK: open it on {{stored}}, as before.\nCancel: open it on {{server}}, in this tab only.": "此链接在 {{server}} 上打开“{{name}}”，但您的项目列表中它位于 {{stored}}。指向其他服务器的链接可能来自运营该服务器的人：只有在您信任它时才在那里打开。\n\n确定：像以前一样在 {{stored}} 上打开。\n取消：在 {{server}} 上打开，仅限此标签页。",
}

export default zh
