import type { Catalog } from '../../i18n'
const zh: Catalog = {
  'That code isn’t valid on this server, or has expired.': '该代码在此服务器上无效，或已过期。',
  'That code hasn’t room for this project: it needs {{need}}, and the code has {{free}} free.': '该代码没有足够空间容纳此项目：需要 {{need}}，而该代码只剩 {{free}}。',
  'This project is larger than a project may be with that code ({{max}}).': '此项目超过了该代码允许的单个项目大小（{{max}}）。',
  'The server doesn’t keep a copy of this project, so there’s nothing to move.': '服务器没有保存此项目的副本，因此无需转移。',
  'The server refused: only the project’s owner can move it.': '服务器拒绝了：只有项目所有者才能转移它。',
  'Too many tries with wrong codes. Reload the page to try again.': '错误代码尝试次数过多。请重新加载页面后再试。',
  'Also use this code for new projects in this browser': '在此浏览器中新建项目时也使用此代码',
  'Move': '转移',
  'Move to another creation code': '转移到另一个创建码',
  'This project’s storage counts against the creation code it was first stored with. Move it to another code, for example when that code has lapsed, or when someone else’s plan should hold the project. Links, collaborators and history stay as they are.': '此项目的存储计入它首次存储时所用的创建码。你可以把它转移到另一个代码，例如原代码已过期，或项目应改由他人的套餐承担时。链接、协作者和历史记录都保持不变。',
}
export default zh
