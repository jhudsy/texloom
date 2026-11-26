import type { Catalog } from '../../i18n'

const zh: Catalog = {
  'Use an AI agent on your computer…': '使用你电脑上的 AI 智能体…',
  'Use an AI agent on your computer': '使用你电脑上的 AI 智能体',
  'Tools that support MCP, such as Claude Code, Claude Desktop or Cursor, can work on this project like a collaborator. They run on your computer under your own account with that tool, so no API key is needed here.': '支持 MCP 的工具（如 Claude Code、Claude Desktop 或 Cursor）可以像协作者一样处理此项目。它们在您的电脑上使用您自己的账户运行，因此这里无需 API 密钥。',
  'How to install uv': '如何安装 uv',
  'Choose what the tool may do': '选择该工具可以执行的操作',
  'Suggest changes for people to accept': '提出更改建议，由他人接受',
  'Edit files directly': '直接编辑文件',
  'Read the project only': '仅读取项目',
  'Add it to your tool': '将其添加到您的工具',
  'These lines contain this project’s link, and your tool saves it in its settings. Anyone who can read those settings can open the project.': '这些内容包含此项目的链接，您的工具会将其保存在设置中。任何能读取这些设置的人都可以打开此项目。',
  'The tool reads text other people wrote in this project, which could include instructions aimed at it. On shared projects, suggesting is the safer choice.': '该工具会读取他人在此项目中写的文字，其中可能包含针对它的指令。在共享项目中，选择“提出建议”更安全。',
  'This is separate from the Assistant tab: you talk to the tool in its own window, not there, and the two don’t share conversations. What the tool does appears in the project under its name, like a collaborator’s work.': '这与“助手”标签页是分开的：你在该工具自己的窗口里与它对话，而不是在那里，两者也不共享对话。该工具所做的事会以它的名字显示在项目中，就像协作者的工作一样。',
  'If your tool can’t find the texloom command, give its full path instead.': '如果你的工具找不到 texloom 命令，请改用其完整路径。',
  'If this project is also in a folder on this computer, you can use the folder’s path instead of the link.': '如果此项目也位于这台电脑的某个文件夹中，可以用该文件夹的路径代替链接。',
  'What your tool runs': '你的工具运行什么',
  'The TeXLoom app, in Applications: nothing more to install': '“应用程序”中的 TeXLoom 应用：无需再安装',
  'The TeXLoom command-line helper, installed once': 'TeXLoom 命令行助手（安装一次即可）',
  'If the TeXLoom desktop app is on this computer, nothing more needs installing: open this project in the app and choose Use an AI agent on your computer… there, for lines that run the app itself. Otherwise, install the TeXLoom command-line helper once:': '如果这台电脑上装有 TeXLoom 桌面应用，就无需再安装：在应用中打开此项目，并在那里选择“使用你电脑上的 AI 智能体…”，即可得到直接运行该应用的命令。否则，请安装一次 TeXLoom 命令行助手：',
  "If the file already has “mcpServers”, put the “texloom” part inside it.": "如果文件中已有“mcpServers”，请把“texloom”部分放进去。",
  "Run this in a terminal, in the folder you start Claude Code from.": "在终端中、在您启动 Claude Code 的文件夹里运行此命令。",
  "In Claude Desktop, open Settings ▸ Developer ▸ Edit Config, add this to the file, then restart Claude Desktop.": "在 Claude Desktop 中打开 Settings ▸ Developer ▸ Edit Config，将此内容添加到文件中，然后重启 Claude Desktop。",
  "Add this to the file .cursor/mcp.json in your home folder (make it if it isn’t there), then restart Cursor.": "将此内容添加到您主文件夹中的 .cursor/mcp.json 文件（如果没有就新建），然后重启 Cursor。",
  "Add this to the tool’s MCP settings, as its documentation describes.": "按照该工具文档的说明，将此内容添加到它的 MCP 设置中。",
  "The program you want to connect": "您要连接的程序",
  "Another tool": "其他工具",
}

export default zh
