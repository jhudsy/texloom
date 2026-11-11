import type { Catalog } from '../../i18n'

const zh: Catalog = {
  "Couldn’t reach GitHub (offline?).": "无法连接 GitHub（离线？）。",
  "GitHub refused the token: it may have expired. Connect again with a new one.": "GitHub 拒绝了该令牌：它可能已过期。请用新令牌重新连接。",
  "GitHub asks to slow down: trying again later.": "GitHub 要求放慢速度：稍后重试。",
  "The token doesn’t allow this: it needs read and write access to the repository’s contents.": "该令牌不允许此操作：它需要对仓库内容的读写权限。",
  "GitHub doesn’t have that repository, or the token can’t see it.": "GitHub 上没有这个仓库，或者该令牌看不到它。",
  "GitHub answered {{status}}{{why}}.": "GitHub 返回了 {{status}}{{why}}。",
  "That doesn’t look like a GitHub token.": "这看起来不像 GitHub 令牌。",
  "The repository has too many files for TeXLoom to keep in step.": "该仓库文件太多，TeXLoom 无法保持同步。",
  "This project isn’t linked to GitHub.": "此项目未关联到 GitHub。",
}
export default zh
