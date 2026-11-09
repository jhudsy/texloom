import type { Catalog } from '../../i18n'

const zh: Catalog = {
  'Copy of {{name}}': '{{name}} 的副本',
  'Name of the copy': '副本名称',
  'Cancel': '取消',
  'Make a copy': '创建副本',
  'The copy is a new project of your own, with its own links. It starts with the current files and settings, without the history, comments or collaborators, and it counts towards your storage.': '副本是一个属于你的新项目，有自己的链接。它包含当前的文件和设置，不包含历史记录、评论和协作者，并计入你的存储空间。',
  'Name': '名称',
  'Copying…': '正在复制…',
  'its files aren’t available right now. If it has no server copy, someone who has it needs to be online.': '其文件目前不可用。如果它在服务器上没有副本，需要拥有该项目的人在线。',
  'Copying… ({{done}} of {{total}})': '正在复制…（{{done}}/{{total}}）',
  'Couldn’t copy the project: {{err}}': '无法复制项目：{{err}}',
  'The copy is missing {{n}} files whose content isn’t available right now: {{files}}': { other: '副本缺少 {{n}} 个目前无法获取内容的文件：{{files}}' },
}

export default zh
