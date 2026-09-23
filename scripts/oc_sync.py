#!/usr/bin/env python3
"""oc-sync —— 跨设备同步 OpenCode 聊天记录（不需要安装 git）。

原理：OpenCode 的会话是事件溯源（event 表为不可变事件，session/message/part 是投影）。
本工具把本机新事件导出成加密增量包，存进一个 GitHub 私有仓库（当加密文件柜用，
直接走 GitHub HTTP API，不依赖 git），在其他机器上再通过 OpenCode 自带的
/sync/replay 接口灌回本机，由 app 自行重建投影。

命令：
  python oc_sync.py init     首次配置（GitHub 令牌 / 私有仓库 / 共享口令 / 机器名）
  python oc_sync.py pull     拉取云端新事件并导入本机（OpenCode 需在运行）
  python oc_sync.py push     导出本机新事件、加密、上传到 GitHub
  python oc_sync.py sync     先 pull 再 push
  python oc_sync.py status   查看本机与云端的进度
"""

import argparse
import base64
import ctypes
import getpass
import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

APP = "oc-sync"
HOME = Path(os.environ.get("USERPROFILE") or Path.home())
CONFIG_DIR = Path(os.environ["OC_SYNC_CONFIG"]) if os.environ.get("OC_SYNC_CONFIG") else (HOME / ".config" / APP)
CONFIG_FILE = CONFIG_DIR / "config.json"
PASS_FILE = CONFIG_DIR / "passphrase"
TOKEN_FILE = CONFIG_DIR / "token"
BUNDLE_PREFIX = "bundles"
DB_PATH = Path(os.environ["OC_SYNC_DB"]) if os.environ.get("OC_SYNC_DB") else (HOME / ".local" / "share" / "opencode" / "opencode.db")
MAGIC = b"OCSB1"
CHUNK = 500
MAX_BUNDLE_BYTES = 20 * 1024 * 1024
MAX_BUNDLE_EVENTS = 5000
SCRYPT_N = 1 << 15
API = "https://api.github.com"


def die(msg, code=1):
    print("错误：" + str(msg), file=sys.stderr)
    sys.exit(code)


def info(msg):
    print(msg)


# ------------------------------------------------------------------ 加密 -----

def derive_key(passphrase: bytes, salt: bytes) -> bytes:
    return hashlib.scrypt(passphrase, salt=salt, n=SCRYPT_N, r=8, p=1, dklen=32,
                          maxmem=1 << 27)


def _aesgcm(key: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        die("缺少依赖 cryptography，请先运行：python -m pip install cryptography")
    return AESGCM(key)


def encrypt_bytes(plain: bytes, passphrase: bytes) -> bytes:
    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = derive_key(passphrase, salt)
    ct = _aesgcm(key).encrypt(nonce, plain, None)
    return MAGIC + salt + nonce + ct


def decrypt_bytes(blob: bytes, passphrase: bytes) -> bytes:
    if blob[:len(MAGIC)] != MAGIC:
        raise ValueError("不是 oc-sync 加密包")
    salt = blob[len(MAGIC):len(MAGIC) + 16]
    nonce = blob[len(MAGIC) + 16:len(MAGIC) + 28]
    ct = blob[len(MAGIC) + 28:]
    key = derive_key(passphrase, salt)
    return _aesgcm(key).decrypt(nonce, ct, None)


# --------------------------------------------------------------- config ------

def load_config():
    if not CONFIG_FILE.exists():
        die("尚未初始化，请先运行：python oc_sync.py init")
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def load_passphrase() -> bytes:
    if not PASS_FILE.exists():
        die("尚未设置共享口令，请先运行：python oc_sync.py init")
    return PASS_FILE.read_bytes()


def load_token() -> str:
    if not TOKEN_FILE.exists():
        die("尚未设置 GitHub 令牌，请先运行：python oc_sync.py init")
    return TOKEN_FILE.read_text(encoding="utf-8").strip()


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------- GitHub API --------

def gh(method, path, token, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "oc-sync",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
        return resp.status, (json.loads(raw.decode()) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def gh_login(token):
    st, res = gh("GET", "/user", token)
    if st != 200 or not isinstance(res, dict):
        die("GitHub 令牌无效或网络不通（HTTP %s）" % st)
    return res["login"]


def gh_ensure_repo(token, owner, repo):
    st, res = gh("GET", "/repos/%s/%s" % (owner, repo), token)
    if st == 200:
        return res.get("default_branch") or "main"
    if st == 404:
        st2, res2 = gh("POST", "/user/repos", token, {
            "name": repo, "private": True, "auto_init": True,
            "description": "oc-sync encrypted event bundles",
        })
        if st2 not in (200, 201):
            die("创建仓库失败：HTTP %s %s" % (st2, res2))
        return res2.get("default_branch") or "main"
    die("访问仓库失败：HTTP %s %s" % (st, res))


def gh_list_bundles(token, owner, repo, branch):
    st, res = gh("GET", "/repos/%s/%s/contents/%s?ref=%s" % (owner, repo, BUNDLE_PREFIX, branch), token)
    if st == 404:
        return []
    if st != 200 or not isinstance(res, list):
        die("列出远端失败：HTTP %s %s" % (st, res))
    return [x for x in res if x.get("type") == "file"]


def gh_download(token, owner, repo, path):
    st, res = gh("GET", "/repos/%s/%s/contents/%s" % (owner, repo, path), token)
    if st != 200 or not isinstance(res, dict):
        die("下载失败 %s：HTTP %s" % (path, st))
    if res.get("content"):
        return base64.b64decode(res["content"])
    url = res.get("download_url")
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token, "User-Agent": "oc-sync"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def gh_upload(token, owner, repo, branch, path, blob, message):
    body = {"message": message, "content": base64.b64encode(blob).decode("ascii"), "branch": branch}
    st, res = gh("GET", "/repos/%s/%s/contents/%s?ref=%s" % (owner, repo, path, branch), token)
    if st == 200 and isinstance(res, dict):
        body["sha"] = res["sha"]
    st, res = gh("PUT", "/repos/%s/%s/contents/%s" % (owner, repo, path), token, body)
    if st not in (200, 201):
        die("上传失败 %s：HTTP %s %s" % (path, st, res))


# --------------------------------------------------------- 包文件命名 --------

def bundle_name(stamp, machine, agg, minseq, maxseq) -> str:
    return "%s__%s__%s__%d__%d.jsonl.enc" % (stamp, machine, agg, minseq, maxseq)


def parse_bundle_name(name):
    m = re.match(r"^([0-9A-Za-z-]+)__(.+)__(ses_[0-9A-Za-z]+)__(\d+)__(\d+)\.jsonl\.enc$", name)
    if not m:
        return None
    return {"stamp": m.group(1), "machine": m.group(2), "agg": m.group(3),
            "min": int(m.group(4)), "max": int(m.group(5)), "name": name}


def remote_heads(files):
    heads = {}
    for f in files:
        meta = parse_bundle_name(f["name"])
        if meta and meta["max"] > heads.get(meta["agg"], -1):
            heads[meta["agg"]] = meta["max"]
    return heads


# -------------------------------------------------------- OpenCode 本地 DB ----

def db_connect_ro():
    if not DB_PATH.exists():
        die("找不到 OpenCode 数据库：%s" % DB_PATH)
    return sqlite3.connect(DB_PATH.as_uri() + "?mode=ro", uri=True)


def local_heads():
    con = db_connect_ro()
    try:
        rows = con.execute("select aggregate_id, seq from event_sequence").fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        con.close()
    return {agg: int(seq) for agg, seq in rows}


def read_events(agg, after_seq):
    con = db_connect_ro()
    try:
        rows = con.execute(
            "select id, seq, type, data from event where aggregate_id=? and seq>? order by seq",
            (agg, after_seq)).fetchall()
    finally:
        con.close()
    return [{"id": i, "aggregateID": agg, "seq": int(s), "type": t, "data": json.loads(d)}
            for i, s, t, d in rows]


# --------------------------------------------------- OpenCode 本地 HTTP 服务 --

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")
psapi = ctypes.WinDLL("psapi")
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010


class _PROCESS_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("Reserved1", ctypes.c_void_p),
        ("PebBaseAddress", ctypes.c_void_p),
        ("Reserved2", ctypes.c_void_p * 2),
        ("UniqueProcessId", ctypes.c_void_p),
        ("Reserved3", ctypes.c_void_p),
    ]


def _read_ptr(handle, address):
    buf = ctypes.c_void_p()
    read = ctypes.c_size_t()
    ok = kernel32.ReadProcessMemory(handle, ctypes.c_void_p(address), ctypes.byref(buf),
                                    ctypes.sizeof(buf), ctypes.byref(read))
    if not ok or read.value != ctypes.sizeof(buf):
        raise OSError("read ptr failed")
    return buf.value


def _read_env_block(handle, address, chunk=4096, max_bytes=1 << 20):
    data = b""
    while len(data) < max_bytes:
        buf = ctypes.create_string_buffer(chunk)
        read = ctypes.c_size_t()
        ok = kernel32.ReadProcessMemory(handle, ctypes.c_void_p(address + len(data)), buf, chunk,
                                        ctypes.byref(read))
        if not ok:
            break
        data += buf.raw[:read.value]
        if b"\x00\x00\x00\x00" in data or read.value < chunk:
            break
    return data.decode("utf-16-le", errors="ignore")


def read_process_env(pid):
    handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        raise OSError("OpenProcess failed")
    try:
        pbi = _PROCESS_BASIC_INFORMATION()
        length = ctypes.c_ulong()
        if ntdll.NtQueryInformationProcess(handle, 0, ctypes.byref(pbi), ctypes.sizeof(pbi),
                                           ctypes.byref(length)) != 0:
            raise OSError("NtQueryInformationProcess failed")
        params = _read_ptr(handle, pbi.PebBaseAddress + 0x20)
        env_addr = _read_ptr(handle, params + 0x80)
        block = _read_env_block(handle, env_addr)
    finally:
        kernel32.CloseHandle(handle)
    env = {}
    for item in block.split("\x00"):
        if item and "=" in item:
            k, _, v = item.partition("=")
            env[k] = v
    return env


def _list_pids():
    arr = (ctypes.c_ulong * 8192)()
    needed = ctypes.c_ulong()
    if not psapi.EnumProcesses(arr, ctypes.sizeof(arr), ctypes.byref(needed)):
        return []
    return list(arr[:needed.value // ctypes.sizeof(ctypes.c_ulong)])


def _port_for_pid(pid):
    r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    fallback = None
    for line in r.stdout.splitlines():
        if "listening" not in line.lower():
            continue
        parts = line.split()
        if not parts or not parts[-1].isdigit() or int(parts[-1]) != pid:
            continue
        addr = parts[1]
        if ":" not in addr:
            continue
        port = int(addr.rsplit(":", 1)[1])
        if addr.startswith("127.0.0.1"):
            return port
        if fallback is None:
            fallback = port
    return fallback


def find_server():
    for pid in _list_pids():
        try:
            env = read_process_env(pid)
        except OSError:
            continue
        pw = env.get("OPENCODE_SERVER_PASSWORD")
        if pw:
            port = env.get("OPENCODE_SERVER_PORT")
            port = int(port) if port and str(port).isdigit() else _port_for_pid(pid)
            if port:
                return port, pw, env.get("OPENCODE_SERVER_USERNAME", "opencode")
    die("未找到 OpenCode 本地服务，请确认 OpenCode 桌面版正在运行")


class Api:
    def __init__(self, port, password, username="opencode"):
        self.base = "http://127.0.0.1:%d" % port
        token = base64.b64encode(("%s:%s" % (username, password)).encode()).decode()
        self.auth = "Basic " + token

    def post(self, path, payload):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": self.auth, "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=300) as resp:
            raw = resp.read()
        return json.loads(raw.decode("utf-8")) if raw else None


# ------------------------------------------------------------------ 命令 -----

def cmd_init(args):
    if CONFIG_FILE.exists() and not args.force:
        info("已存在配置：%s（要重写请加 --force）" % CONFIG_FILE)
        return
    token = os.environ.get("OC_SYNC_TOKEN") or args.token
    if not token:
        token = getpass.getpass("GitHub 令牌（fine-grained，需 Contents 读写；不会显示）：").strip()
    if not token:
        die("令牌不能为空")
    owner = gh_login(token)
    repo = args.repo or input("数据仓库名（默认 opencode-history-sync-data，不存在会自动建私有库）：").strip()
    if not repo:
        repo = "opencode-history-sync-data"
    branch = gh_ensure_repo(token, owner, repo)
    pw = os.environ.get("OC_SYNC_PASSPHRASE")
    if not pw:
        a = getpass.getpass("设置共享口令（多台机器一致，建议 20+ 字符）：")
        b = getpass.getpass("再输入一遍：")
        if a != b:
            die("两次口令不一致")
        pw = a
    if not pw:
        die("口令不能为空")
    machine = (args.machine or input("本机名字（默认 %s）：" % socket.gethostname())).strip()
    machine = re.sub(r"[^A-Za-z0-9_-]", "-", machine or socket.gethostname())
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(token, encoding="utf-8")
    PASS_FILE.write_bytes(pw.encode("utf-8"))
    save_config({"owner": owner, "repo": repo, "branch": branch, "machine": machine})
    info("初始化完成：%s/%s（分支 %s），机器名 %s" % (owner, repo, branch, machine))
    info("下一步：任意一台先 push 一次全量，其余机器 pull。")


def cmd_push(args):
    cfg = load_config()
    token, pw = load_token(), load_passphrase()
    files = gh_list_bundles(token, cfg["owner"], cfg["repo"], cfg["branch"])
    heads = remote_heads(files)
    local = local_heads()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + os.urandom(3).hex()
    total = uploads = 0
    for agg, head in sorted(local.items()):
        after = heads.get(agg, -1)
        if head <= after:
            continue
        events = read_events(agg, after)
        if not events:
            continue
        buf = bytearray()
        first_seq = events[0]["seq"]
        last_seq = first_seq
        for ev in events:
            line = json.dumps(ev, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
            buf += line
            last_seq = ev["seq"]
            if len(buf) >= MAX_BUNDLE_BYTES or (last_seq - first_seq + 1) >= MAX_BUNDLE_EVENTS:
                name = bundle_name(stamp, cfg["machine"], agg, first_seq, last_seq)
                gh_upload(token, cfg["owner"], cfg["repo"], cfg["branch"],
                          "%s/%s" % (BUNDLE_PREFIX, name), encrypt_bytes(bytes(buf), pw),
                          "sync %s: %s" % (cfg["machine"], name))
                uploads += 1
                buf = bytearray()
                first_seq = None
        if buf:
            name = bundle_name(stamp, cfg["machine"], agg, first_seq, last_seq)
            gh_upload(token, cfg["owner"], cfg["repo"], cfg["branch"],
                      "%s/%s" % (BUNDLE_PREFIX, name), encrypt_bytes(bytes(buf), pw),
                      "sync %s: %s" % (cfg["machine"], name))
            uploads += 1
        total += len(events)
    if total == 0:
        info("本机没有云端缺少的新事件，无需推送。")
        return
    info("已上传 %d 条新事件（%d 个加密包）。" % (total, uploads))


def _replay_run(api, run, directory):
    for i in range(0, len(run), CHUNK):
        api.post("/sync/replay", {"directory": directory or "", "events": run[i:i + CHUNK]})


def cmd_pull(args):
    cfg = load_config()
    token, pw = load_token(), load_passphrase()
    files = gh_list_bundles(token, cfg["owner"], cfg["repo"], cfg["branch"])
    local = local_heads()
    needed = []
    for f in files:
        meta = parse_bundle_name(f["name"])
        if meta and meta["max"] > local.get(meta["agg"], -1):
            meta["file"] = f
            needed.append(meta)
    if not needed:
        info("云端没有本机缺少的新事件。")
        return
    by_agg = {}
    for meta in needed:
        blob = gh_download(token, cfg["owner"], cfg["repo"], "%s/%s" % (BUNDLE_PREFIX, meta["name"]))
        for line in decrypt_bytes(blob, pw).split(b"\n"):
            if line.strip():
                ev = json.loads(line.decode("utf-8"))
                by_agg.setdefault(ev["aggregateID"], {})[ev["id"]] = ev
    port, spw, user = find_server()
    api = Api(port, spw, user)
    applied = sessions = 0
    for agg, evmap in by_agg.items():
        byseq = {}
        for ev in evmap.values():
            byseq.setdefault(ev["seq"], ev)
        run, expect = [], local.get(agg, -1) + 1
        while expect in byseq:
            run.append(byseq[expect])
            expect += 1
        if not run:
            continue
        directory = ""
        for ev in run:
            d = ev.get("data", {}).get("info", {}).get("directory")
            if d:
                directory = d
                break
        _replay_run(api, run, directory)
        applied += len(run)
        sessions += 1
    if applied == 0:
        info("没有可导入的连续事件（可能缺少更早的包，等补齐后再试）。")
    else:
        info("已导入 %d 条事件，覆盖 %d 个会话。" % (applied, sessions))
        info("若界面没立刻刷新，切换一下会话或重启 OpenCode 即可。")


def cmd_sync(args):
    cmd_pull(args)
    cmd_push(args)


def cmd_status(args):
    cfg = load_config()
    token = load_token()
    files = gh_list_bundles(token, cfg["owner"], cfg["repo"], cfg["branch"])
    heads = remote_heads(files)
    local = local_heads()
    ml = mr = 0
    for agg in set(local) | set(heads):
        ml += max(0, heads.get(agg, -1) - local.get(agg, -1))
        mr += max(0, local.get(agg, -1) - heads.get(agg, -1))
    info("本机会话数：%d，云端已收录会话数：%d" % (len(local), len(heads)))
    info("待从云端导入：%d 条事件" % ml)
    info("待推送到云端：%d 条事件" % mr)


def main():
    if os.name != "nt":
        die("当前只支持 Windows 桌面版 OpenCode")
    p = argparse.ArgumentParser(prog="oc-sync", description="OpenCode 聊天记录跨设备同步（GitHub 私有库 + 端到端加密，无需 git）")
    sub = p.add_subparsers(dest="cmd")
    pi = sub.add_parser("init", help="首次配置（令牌 / 仓库 / 口令）")
    pi.add_argument("--token", help="GitHub 令牌（否则交互输入）")
    pi.add_argument("--repo", help="数据仓库名")
    pi.add_argument("--machine", help="本机名字")
    pi.add_argument("--force", action="store_true", help="覆盖已有配置")
    pi.set_defaults(func=cmd_init)
    sub.add_parser("push", help="导出本机新事件并上传").set_defaults(func=cmd_push)
    sub.add_parser("pull", help="拉取并导入云端新事件").set_defaults(func=cmd_pull)
    sub.add_parser("sync", help="先 pull 再 push").set_defaults(func=cmd_sync)
    sub.add_parser("status", help="查看进度").set_defaults(func=cmd_status)
    args = p.parse_args()
    if not args.cmd:
        p.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
