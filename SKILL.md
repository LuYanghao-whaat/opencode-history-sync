---
name: opencode-history-sync
description: 让 OpenCode 桌面版的聊天记录/会话历史跨设备同步（换台电脑历史还在），不依赖任何云服务器或厂商云同步，用私有 git 仓库做端到端加密中转。当用户提到「OpenCode 聊天记录同步」「换台电脑历史没了/不见了」「会话跨设备」「同步 opencode 历史/会话」「备份 opencode 聊天」「把 opencode 历史迁到新电脑」「多台电脑共用 opencode 记录」时，务必使用本技能。英文触发词：sync OpenCode chat history across devices、backup/migrate OpenCode sessions、OpenCode history missing on another computer、share OpenCode sessions between machines。也适用于用户想备份、迁移或合并多台机器的 OpenCode 会话。
---

# OpenCode 聊天记录跨设备同步

把 OpenCode 桌面版的会话历史在多台电脑之间同步：换机器后历史还在、边用边积累，全程不经过任何厂商的云同步，也不需要一个常开的服务器。

## 一句话原理

OpenCode 的会话是**事件溯源**结构：`event` 表里是一条条**不可变**事件（唯一 `evt_` id、按会话 `seq` 递增），`session`/`message`/`part` 只是重放这些事件算出来的投影。所以同步的正确做法是**搬运并合并事件**，而不是拷贝那个几百 MB 的 `opencode.db`（运行中拷贝会因 `-wal` 损坏，且整库覆盖会丢另一台的记录）。

本技能用 OpenCode 桌面版**自带的两个本地接口**做导入导出：

- `POST /sync/history`：告诉它「每个会话我已有到第几页」，返回本机缺的事件。
- `POST /sync/replay`：把事件灌回本机；它自己校验 `seq` 连续、内容一致，然后重建投影。

中间的传输用一个**私有 git 仓库**（存储转发：不需要任何机器常开、不需要同一局域网），落盘前用**共享口令端到端加密**，托管方只看到密文。

详细机制、验证依据与限制见 `references/how-it-works.md`。

## 前置条件

- **Windows**（工具通过读取 OpenCode 服务进程环境变量拿本地 API 密码，仅 Windows 实现）。
- 每台机器：**Python 3.8+**、**git**、Python 包 `cryptography`（`python -m pip install cryptography`）。
- 一个 git 托管账号，能建**私有**仓库（推荐 Codeberg；GitHub/GitLab 皆可）。
- OpenCode 桌面版在 **pull/sync 时必须处于运行状态**（replay 走它的本地 API）；push 只读本机 SQLite，不需要它开着。

## 操作流程（照着做）

### 1. 建私有仓库并拿到令牌

用任意一家：

- **Codeberg**（推荐，非营利、无追踪）：注册 → 新建仓库，**勾选「将仓库设为私有」** → 头像 → Settings → Applications → Generate New Token，勾 `write:repository`（必要时加 `read:repository`），复制令牌。仓库地址形如 `https://codeberg.org/<用户名>/<仓库>.git`。
- **GitHub**：New repository → **Private** → Settings → Developer settings → Personal access tokens，勾 `repo`（或 fine-grained 的 Contents: Read and write）。
- **GitLab**：新建 Private 项目 → Settings → Access tokens，勾 `write_repository`。

令牌只显示一次，先存好。**不要**把令牌写进任何会被提交的文件。

### 2. 放置工具

把本技能的 `scripts/oc_sync.py` 拷到每台机器（例如 `Documents\oc-sync\oc_sync.py`）。它单文件、无第三方依赖（只用到 `cryptography`）。

先看用法：

```powershell
python oc_sync.py --help
```

### 3. 第一台机器：初始化并首次全量上传

```powershell
python oc_sync.py init
```

按提示填：仓库地址、**共享口令**（5 台机器一致，建议 20+ 字符，别外泄）、本机名字。

首次 `git clone` 私有库时 git 会要求登录：用户名填 git 平台用户名，**密码填令牌**。Git 凭据管理器会记住，后续不用再输。

然后全量推送（事件只增不改，首次数据量最大，可能几百 MB，属正常）：

```powershell
python oc_sync.py push
```

### 4. 其余机器：初始化并下载补齐

同样先 `init`（填同一个仓库地址、**同一个共享口令**），然后：

```powershell
python oc_sync.py pull
```

看到「已导入 N 条事件」后，OpenCode 界面里就会出现同步过来的会话（若没立刻刷新，切换一下会话或重启 OpenCode）。

### 5. 日常使用

```powershell
python oc_sync.py pull    # 到一台机器：把仓库里的新事件导入本机
python oc_sync.py push    # 离开前：把本机新事件加密推送到仓库
python oc_sync.py sync    # 先 pull 再 push（最省事）
python oc_sync.py status  # 看两边进度差多少
```

## 常见问题

- **`pull` 报找不到 OpenCode 本地服务**：先把 OpenCode 桌面版打开再 pull。
- **`pull` 后界面没变化**：切换一下会话，或重启 OpenCode。事件已入库，只是界面没实时刷新。
- **`git push` 被拒（non-fast-forward）**：正常，工具会自动 `pull --rebase` 后重试；多推几次即可。不同机器的包文件名带时间戳和机器名，不会冲突。
- **缺少 `cryptography`**：`python -m pip install cryptography`。
- **导入后打开会话提示目录不存在**：见下条限制，不影响阅读历史。

## 限制与注意（重要）

- **会话目录是「源机器」的绝对路径**：事件里记的是原机器的目录（如 `C:\Users\A\proj`）。同步到别的机器后，**历史消息完好、可读**，但若该路径在目标机不存在，打开会话的文件树/LSP 会不对、也不能直接在原目录继续干活。想无缝续写，就让多台机器使用相同的项目路径。
- **共享口令泄露一台 = 全泄**：务必用强口令、不要在机器间以外的地方存放。要更换口令需在所有机器重新 `init` 并重新加密历史。
- **托管方只能看到密文**，但能看到提交时间、包体积等元数据。
- **不要**直接对 `opencode.db` 做文件级同步/覆盖（本技能刻意避开这一点）。
- 首次全量体积取决于历史里工具输出的多少；之后每次只推增量。

## 参考

- `references/how-it-works.md`：事件溯源、`/sync/history` 与 `/sync/replay` 的行为与校验、为什么这样合并是安全的、目录限制的代码依据。
- `scripts/oc_sync.py`：同步工具（`--help` 查看命令）。
