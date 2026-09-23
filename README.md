# opencode-history-sync

让 **OpenCode 桌面版**的聊天记录/会话历史在**多台电脑之间同步**：换机器后历史还在，边用边积累。全程**不依赖任何云服务器或厂商云同步**，也不需要常开的机器——用一个 **GitHub 私有仓库**做端到端加密的中转。

> 这是一个面向 **AI Agent** 的 Skill：把本目录放进你的 skills 目录，AI 助手在触发时会**照着 `SKILL.md` 自己动手**完成配置与同步（自动检测环境、装依赖、跑命令、自检），只在必须本人操作的地方（生成/输入令牌、输入口令）让你介入。**不需要安装 git。**

## 它解决什么问题

OpenCode 的聊天记录默认只存在本机（`%USERPROFILE%\.local\share\opencode\opencode.db`），换台电脑历史就没了。本技能用「事件级同步」把会话历史搬到任何你想要的机器上。

## 原理（一句话）

OpenCode 的会话是**事件溯源**：`event` 表是不可变事件，`session`/`message`/`part` 只是重放出来的投影。所以同步是**搬运并合并事件**（用 OpenCode 自带的 `/sync/history` 与 `/sync/replay` 接口），而不是拷贝那个几百 MB 的数据库文件。加密包通过 **GitHub HTTP API** 存进私有仓库（不装 git、不需要登录 GitHub 账号），落盘前用共享口令 **AES-256-GCM** 加密，GitHub 只看到密文。

细节见 [`references/how-it-works.md`](references/how-it-works.md)。

## 安装为 Skill

把整个目录放进你的 OpenCode skills 目录（Windows 通常是 `%USERPROFILE%\.config\opencode\skills\`）：

```
%USERPROFILE%\.config\opencode\skills\opencode-history-sync\
├── SKILL.md
├── references/how-it-works.md
└── scripts/oc_sync.py
```

重启 OpenCode 后，当你提到「OpenCode 聊天记录同步 / 换电脑历史没了 / 同步会话」时会自动触发。

## 快速开始（也可手动照做）

**前置**：Windows、Python 3.8+、`python -m pip install cryptography`，以及一个 **GitHub fine-grained 令牌**（Contents: Read and write）。

1. 生成一个 fine-grained PAT（Contents 读写）。**不需要在电脑上登录 GitHub。**
2. 每台机器放好 `scripts/oc_sync.py`，然后：

```powershell
python oc_sync.py init      # 输入令牌、仓库名（不存在会自动建私有库）、共享口令（多台一致）、本机名
python oc_sync.py push      # 第一台：全量上传
python oc_sync.py pull      # 其余机器：下载补齐（需 OpenCode 在运行）
python oc_sync.py sync      # 日常：先 pull 再 push
```

## 限制

- 仅 **Windows**。
- `pull`/`sync` 需要 OpenCode 桌面版在运行。
- 同步过来的会话保留**源机器**的绝对目录路径：消息可读，但若目标机没有该路径，打开会话的目录相关功能会不对。多台机器用相同项目路径即可无缝续写。
- 共享口令泄露一台 = 全泄；令牌同理，泄露即撤销重发。
- **不要**直接对 `opencode.db` 做文件级同步/覆盖。

## 安全

- 仓库里只有密文事件包 + 文件名元数据；GitHub 看不到聊天内容。
- 令牌越权最小化：fine-grained PAT 只给目标仓库 Contents 读写。
- 令牌/口令存本机 `%USERPROFILE%\.config\oc-sync\`，不进仓库、不写日志。

## License

MIT
