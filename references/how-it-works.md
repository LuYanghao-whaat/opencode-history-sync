# 工作原理与验证依据

本文件解释「为什么这样做」，以及工具依赖的 OpenCode 行为是怎么确认的。普通使用只需读 `SKILL.md`。

## 1. OpenCode 的存储是事件溯源

本机数据库在 Windows 上是 `%USERPROFILE%\.local\share\opencode\opencode.db`（SQLite）。关键表：

| 表 | 作用 |
|---|---|
| `event` | **事实来源**。列：`id`(主键，形如 `evt_...`)、`aggregate_id`(会话 id，`ses_...`)、`seq`(该会话内递增序号，从 0 开始)、`type`(如 `session.created.1`)、`data`(JSON)。 |
| `event_sequence` | 每个会话当前的日志头（`aggregate_id` 主键、`seq`、`owner_id`）。 |
| `session` / `message` / `part` / `todo` | **投影（读模型）**，由事件重放得出。 |

事件**不可变**；同一会话内 `seq` 连续、每次只 +1。数据库有唯一索引 `(aggregate_id, seq)`。

结论：**同步 = 搬运事件 + 让 app 重放**，而不是拷贝整个 `.db`。直接文件级拷贝/覆盖在 OpenCode 运行时不可靠（WAL），也会丢掉另一台机器的新记录。

## 2. 用到的本机接口

OpenCode 桌面版本地起了一个 HTTP 服务，**只监听 `127.0.0.1`**，用 Basic 认证（用户名默认 `opencode`，密码是服务进程环境变量 `OPENCODE_SERVER_PASSWORD`）。它暴露了同步接口：

### `POST /sync/history`

- 请求体：`{ "<aggregate_id>": <已知最大 seq>, ... }`（即「我已有的进度」）。
- 返回：所有**不满足**「属于某个已给的会话且 `seq <= 该会话已给值`」的事件，按 `seq` 升序，形如
  `[{ "id": "...", "aggregate_id": "...", "seq": N, "type": "...", "data": {...} }]`。
- 空请求体 `{}` 会返回全部事件（首次导出即用这个）。

### `POST /sync/replay`

- 请求体：`{ "directory": "...", "events": [ { "id", "aggregateID", "seq", "type", "data" }, ... ] }`。
- 行为：把事件灌进本机事件库，**自己校验**后重建投影，返回 `{ "sessionID": "<第一个事件的 aggregateID>" }`。
- 校验规则（来自其 `replayAll` / `commitDurableEvent`）：
  - 一次请求里的所有事件**必须属于同一个会话**；
  - `seq` 必须从第一个事件起**严格连续**（`first, first+1, ...`）；
  - 对每个事件：若 `seq <= 本机该会话当前头`，则必须与已存事件**完全相同**（相同 `id`/`type`/`data`）——相同则视为重放、无副作用；不同则**报错拒绝**（`Replay diverged ...`），不会静默覆盖；
  - 若 `seq == 头 + 1`，追加并重放；若 `seq > 头 + 1`，报 `Sequence mismatch` 拒绝。

这正是「移动会话到工作区」用的接口，因此是官方支持的路径。工具据此：只发送**从本机断点开始的连续段**，遇到缺口就停，等缺的包到了再发——绝不硬塞。

### 关于 owner / 工作区

事件库有 `owner_id` 概念（用于把会话归属到某个「工作区」）。**不配置工作区时该字段为空**，replay 的 `strictOwner` 校验不会触发，事件可正常落库。因此本方案**不需要**任何工作区或云端账号。若将来启用了工作区，行为需另行评估。

## 3. 传输：GitHub 私有仓库当「加密文件柜」（无需 git）

- 每台机器把「本机新事件」写成**一包一包的加密文件**，通过 **GitHub HTTP API** 上传到私有仓库的 `bundles/` 目录；换机器时列出并下载。
- 用到的接口：`GET /repos/{o}/{r}/contents/bundles?ref=<branch>`（列目录）、`GET /repos/{o}/{r}/contents/<path>`（取文件，>1MB 时走 `download_url` 原样下载）、`PUT /repos/{o}/{r}/contents/<path>`（上传，已存在则带 `sha` 覆盖）、`POST /user/repos`（首次自动建私有库）。
- 认证只用 **fine-grained PAT**（Contents: Read and write），**不需要安装 git，也不需要电脑登录 GitHub 账号**。
- 文件名形如 `bundles/<时间戳-随机>__<机器名>__<会话id>__<最小seq>__<最大seq>.jsonl.enc`。文件名即索引：判断某会话有没有新包、云端各会话进度到哪，都无需解密整包。
- 单包上限约 20MB / 5000 事件，超了会**自动切成多个包**（同一会话分成若干连续的 `seq` 段），导入时按 `seq` 拼回。
- 文件名唯一，天然**无冲突**；托管服务总在线，**任意一台随时可同步**，不需要任何设备常开、不需要同一局域网。

## 4. 加密

- 共享口令（5 台一致）+ `scrypt`(n=2^15,r=8,p=1, 盐随包随机) 派生 32 字节密钥 → **AES-256-GCM** 加密整包。
- 包格式：魔数 `OCSB1` + 盐(16) + nonce(12) + 密文。
- 托管方只能看到密文与元数据（提交时间、包大小）。**共享口令泄露一台 = 全泄**，务必用强口令。

## 5. 已知限制

- **仅 Windows**：工具通过读取 OpenCode 服务进程的 PEB 环境块获得本地 API 密码（该密码不落盘、不在命令行）。
- **pull/sync 需要 OpenCode 在运行**（要调它的本地接口）；push 只读 SQLite，不需要。
- **会话目录保持源机器的绝对路径**：投影里会话的 `directory` 取自事件 `data.info.directory`；replay 请求里的 `directory` 字段只写日志、不参与落库。因此在目标机若没有该路径：历史消息照常可读，但打开会话的目录相关功能会不对。让多台机器用相同项目路径即可无缝续写。
- 首次全量体积取决于历史中工具输出（可能数百 MB）；之后只推增量。

## 6. 附：本地服务的发现方式

1. 枚举进程，读其环境块，找含有 `OPENCODE_SERVER_PASSWORD` 的进程（即 OpenCode 的 NodeService）。
2. 用它 PID 从 `netstat -ano` 找本地监听端口。
3. 用 `opencode : <密码>` 做 Basic 认证访问 `http://127.0.0.1:<port>`。

这些都在 `scripts/oc_sync.py` 内实现，无需用户手工操作。
