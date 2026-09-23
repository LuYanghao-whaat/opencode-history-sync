---
name: opencode-history-sync
description: 让 OpenCode 桌面版的聊天记录/会话历史跨设备同步（换台电脑历史还在），不依赖任何云服务器或厂商云同步，用私有 git 仓库做端到端加密中转。当用户提到「OpenCode 聊天记录同步」「换台电脑历史没了/不见了」「会话跨设备」「同步 opencode 历史/会话」「备份 opencode 聊天」「把 opencode 历史迁到新电脑」「多台电脑共用 opencode 记录」「帮我同步一下聊天记录」时，务必使用本技能，并由你（助手）直接执行本手册里的操作，而不是只讲给用户听。英文触发词：sync OpenCode chat history across devices、backup/migrate OpenCode sessions、OpenCode history missing on another computer、share OpenCode sessions between machines。
---

# OpenCode 聊天记录跨设备同步 —— Agent 执行手册

你是执行者。触发本技能后，**由你动手做完**：检测环境、装依赖、调用工具、读输出、自检、汇报。只有**必须人类本人操作**的步骤（注册/建私有仓库、输入访问令牌、输入共享口令）才交给用户，其余不要甩给用户。

## 你会用到的资源

- `scripts/oc_sync.py` —— 同步工具（单文件，Windows，Python 3.8+，依赖 `cryptography`）。
- `references/how-it-works.md` —— 机制与校验规则；**调试或用户追问原理时再读**。

脚本路径按本 SKILL.md 所在目录拼接，命令统一用 `python "<...>\scripts\oc_sync.py" ...`。

## 心智模型（决定你怎么做）

OpenCode 的会话是**事件溯源**：`event` 表是不可变事件，`session`/`message`/`part` 是重放出来的投影。所以同步是**搬运并合并事件**，不是拷贝 `opencode.db`。工具用 OpenCode 桌面版**自带的本地接口**导出/导入，用**私有 git 仓库**做存储转发，**共享口令 + AES-256-GCM** 加密。

**两条铁律**：
1. **绝不**对 `opencode.db` 做文件级同步/覆盖，也绝不读写、打印、提交用户的令牌或口令。
2. **未经用户明确同意，不要执行 `push`**（会把本机聊天记录上传到仓库）。

## 先判断是哪类任务

- 用户机器上还不存在 `%USERPROFILE%\.config\oc-sync\config.json` → **任务 A：首次配置**。
- 已存在 → **任务 B：日常同步**。

（不确定就先 `python "…\oc_sync.py" status`，能跑通即已配置。）

---

## 任务 A：首次配置

### A1. 环境自检（你直接执行）

依次运行并检查：

1. Windows：`python -c "import os;print(os.name)"` 应为 `nt`。非 Windows 直接告诉用户本技能暂只支持 Windows。
2. Python：`python --version`（需 3.8+）。
3. git：`git --version`。
4. 加密依赖：`python -c "import cryptography"`；**报错就装**：`python -m pip install cryptography`。

任一缺失且装不上，先解决再继续。

### A2. 让用户提供三样东西（这是唯一需要人类的部分）

用对话问清，**不要**让用户把令牌或口令贴进聊天窗口：

1. **私有仓库地址**：形如 `https://codeberg.org/<用户名>/<仓库>.git`。
   - 用户还没有仓库时，给出建库指引：推荐 Codeberg（Codeberg → New repository → **勾选「设为私有」** → 生成 Access Token，勾 `write:repository`）。GitHub/GitLab 同理（私有仓库 + 具备仓库读写权限的令牌）。
2. **共享口令**：多台机器保持一致。用户没有就让他定一个（建议 20+ 字符）。**让用户在 init 时自己输入，别让他发给你。**
3. **本机名字**：默认用主机名即可。

### A3. 引导用户在本机终端跑一次 init（含密钥输入，必须由用户本人完成）

因为要从 git 弹窗输入令牌、并在无回显下输入口令，**让用户在他自己的终端**执行（你把命令拼好给他）：

```powershell
python "…\oc_sync.py" init
```

（可预设非机密项减少提问：`python "…\oc_sync.py" init --repo "<仓库地址>" --machine "<本机名>"`。）

用户跑完后，你**验证**：`config.json` 与 `passphrase` 是否已在 `%USERPROFILE%\.config\oc-sync\` 下生成；然后运行 `status` 确认能连通（会显示本机会话数等）。若 init 报错（认证失败、仓库不存在），据错误信息帮用户修。

### A4. 首次上传（先征得同意，再执行）

向用户说明首次会上传**本机全部历史**（体积可能几百 MB）并**得到明确同意**后，你执行：

```powershell
python "…\oc_sync.py" push
```

### A5. 其余机器

每台机器同样走 A1、A2、A3（**同一个仓库地址、同一个口令**），然后你执行：

```powershell
python "…\oc_sync.py" pull
```

`pull` 需要 OpenCode 正在运行；若报「找不到 OpenCode 本地服务」，让用户打开桌面版后重试。导入完成后提醒：界面若没立刻刷新，切换一下会话或重启 OpenCode。

---

## 任务 B：日常同步（你直接执行，无需用户动手）

用户说「同步一下 / 备份一下 / 拉一下记录 / 推到仓库」时：

1. 若要做 `pull`/`sync`，先确认 OpenCode 在运行（否则找得到服务才怪）。
2. 执行：
   ```powershell
   python "…\oc_sync.py" sync      # 默认：先 pull 再 push
   ```
   只想单向就 `pull` 或 `push`。**`push` 前仍要确认用户同意上传。**
3. 读输出并向用户一句话汇报：导入了多少条、覆盖几个会话 / 推送了多少条。
4. 收尾提醒：pull 后界面没刷新就切会话或重启 OpenCode。

若 `push` 报 non-fast-forward：工具会自动 `pull --rebase` 重试，属正常；连续失败就把完整错误给用户。

---

## 故障排查

- **`pull` 报找不到本地服务**：OpenCode 桌面版没开；让用户打开后重试。
- **导入后界面没变**：事件已入库，界面没实时刷新；切会话或重启。
- **缺 `cryptography`**：`python -m pip install cryptography`。
- **导入后打开会话提示目录不存在**：正常，见下方限制；历史消息可读。
- **`init` 后 `status` 连不上**：多为仓库地址或令牌权限问题；核对地址与令牌权限（Codeberg 需 `write:repository`）。

## 限制（汇报时要如实告诉用户）

- **仅 Windows**（工具需读取 OpenCode 服务进程环境变量拿本地 API 密码）。
- **pull/sync 需要 OpenCode 在运行**；push 只需要本机 SQLite（读取，不修改）。
- **同步来的会话保留源机器的绝对目录**：消息可读，但目标机没有该路径时，打开会话的文件树/LSP 会不对，也不能就地续写。多台机器用相同项目路径即可无缝续写。
- **共享口令泄露一台 = 全泄**；务必强口令、勿外泄。
- 首次全量体积取决于历史中工具输出；之后只推增量。
- 托管方只能看到密文与元数据（提交时间、包体积）。

## 附：判断依据

事件表结构、`/sync/history` 与 `/sync/replay` 的请求/响应与校验规则（同会话、`seq` 严格连续、不一致会拒绝而非覆盖）、owner 行为、目录限制的代码依据，全部见 `references/how-it-works.md`。用户若质疑方案是否安全，引用该文件作答。
