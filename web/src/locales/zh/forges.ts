import type { Catalog } from '../../i18n'

const zh: Catalog = {
  "Couldn’t reach {{service}} (offline?).": "无法连接 {{service}}（离线？）。",
  "{{service}} refused the token: it may have expired. Connect again with a new one.": "{{service}} 拒绝了令牌：它可能已过期。请用新令牌重新连接。",
  "{{service}} asks to slow down: trying again later.": "{{service}} 要求放慢速度：稍后重试。",
  "{{service}} doesn’t have that repository, or the token can’t see it.": "{{service}} 上没有该仓库，或令牌无权查看。",
  "{{service}} answered {{status}}{{why}}.": "{{service}} 返回 {{status}}{{why}}。",
  "Nothing to send.": "没有要发送的内容。",
  "{{service}} has new commits: bringing them in first.": "{{service}} 有新的提交：先将其拉取进来。",
}
export default zh
