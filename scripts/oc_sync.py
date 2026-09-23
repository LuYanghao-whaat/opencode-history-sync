#!/usr/bin/env python3
"""oc-sync —— 在不开云服务器、不用厂商云同步的前提下，跨设备同步 OpenCode 聊天记录。

原理：OpenCode 的会话是事件溯源（event 表为不可变事件，session/message/part 是投影）。
本工具把本机新事件导出成加密增量包存进一个私有 git 仓库（存储转发），
在其他机器上再通过 OpenCode 自带的 /sync/replay 接口灌回本机，由 app 自行重建投影。

命令：
  python oc_sync.py init     首次配置（仓库地址 / 共享口令 / 机器名），并克隆仓库
  python oc_sync.py pull     拉取仓库新事件并导入本机（OpenCode 需在运行）
  python oc_sync.py push     导出本机新事件、加密、提交并推送到仓库
  python oc_sync.py sync     先 pull 再 push
  python oc_sync.py status   查看本机与仓库的事件进度
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
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

APP = "oc-sync"
HOME = Path(os.environ.get("USERPROFILE") or Path.home())
CONFIG_DIR = Path(os.environ["OC_SYNC_CONFIG"]) if os.environ.get("OC_SYNC_CONFIG") else (HOME / ".config" / APP)
CONFIG_FILE = CONFIG_DIR / "config.json"
PASS_FILE = CONFIG_DIR / "passphrase"
REPO_DIR = CONFIG_DIR / "repo"
BUNDLE_DIR_NAME = "bundles"
DB_PATH = Path(os.environ["OC_SYNC_DB"]) if os.environ.get("OC_SYNC_DB") else (HOME / ".local" / "share" / "opencode" / "opencode.db")
MAGIC = b"OCSB1"
CHUNK = 500
SCRYPT_N = 1 << 15


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


# --------------------------------------------------------------- 本机 config --

def load_config():
    if not CONFIG_FILE.exists():
        die("尚未初始化，请先运行：python oc_sync.py init")
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def load_passphrase() -> bytes:
    if not PASS_FILE.exists():
        die("尚未设置共享口令，请先运行：python oc_sync.py init")
    return PASS_FILE.read_bytes()


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------------------------------------------------------- git -----

def git(*args, check=True):
    r = subprocess.run(
        ["git"] + list(args), cwd=str(REPO_DIR),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if check and r.returncode != 0:
        die("git %s 失败：\n%s%s" % (" ".join(args), r.stdout, r.stderr))
    return r


def ensure_repo():
    cfg = load_config()
    repo = cfg["repo"]
    if (REPO_DIR / ".git").exists():
        git("remote", "set-url", "origin", repo)
    else:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["git", "clone", repo, str(REPO_DIR)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            git("init")
            git("remote", "add", "origin", repo)
    git("config", "user.name", cfg.get("machine") or "oc-sync", check=False)
    git("config", "user.email", (cfg.get("machine") or "oc-sync") + "@oc-sync.local", check=False)
    git("config", "push.default", "current", check=False)
    git("config", "pull.rebase", "true", check=False)
    return cfg


def git_pull():
    r = subprocess.run(
        ["git", "pull", "--rebase", "--autostash"], cwd=str(REPO_DIR),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        out = r.stdout + r.stderr
        benign = ("couldn't find remote ref", "no commits yet", "no such ref was fetched")
        if any(s in out for s in benign):
            return
        die("git pull 失败：\n%s" % out)


def git_commit_push(message):
    git("add", "-A")
    r = git("commit", "-m", message, check=False)
    if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
        die("git commit 失败：\n%s%s" % (r.stdout, r.stderr))
    for _ in range(3):
        r = git("push", "-u", "origin", "HEAD", check=False)
        if r.returncode == 0:
            return
        git_pull()
    die("git push 多次失败：\n%s%s" % (r.stdout, r.stderr))


# ------------------------------------------------------------------ 包文件 ----

def bundle_dir() -> Path:
    return REPO_DIR / BUNDLE_DIR_NAME


def bundle_name(stamp, machine, agg, minseq, maxseq) -> str:
    return "%s__%s__%s__%d__%d.jsonl.enc" % (stamp, machine, agg, minseq, maxseq)


def parse_bundle_name(name):
    m = re.match(r"^(\d{8}T\d{6}Z)__(.+)__(ses_[0-9A-Za-z]+)__(\d+)__(\d+)\.jsonl\.enc$", name)
    if not m:
        return None
    return {
        "stamp": m.group(1), "machine": m.group(2), "agg": m.group(3),
        "min": int(m.group(4)), "max": int(m.group(5)),
    }


def scan_bundles():
    d = bundle_dir()
    out = []
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.jsonl.enc")):
        meta = parse_bundle_name(p.name)
        if meta:
            meta["path"] = p
            out.append(meta)
    return out


def repo_heads():
    heads = {}
    for b in scan_bundles():
        if b["max"] > heads.get(b["agg"], -1):
            heads[b["agg"]] = b["max"]
    return heads


# -------------------------------------------------------- OpenCode 本地 DB ----

def db_connect_ro():
    if not DB_PATH.exists():
        die("找不到 OpenCode 数据库：%s" % DB_PATH)
    uri = DB_PATH.as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


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
            "select id, seq, type, data from event "
            "where aggregate_id=? and seq>? order by seq",
            (agg, after_seq),
        ).fetchall()
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
    n = needed.value // ctypes.sizeof(ctypes.c_ulong)
    return list(arr[:n])


def _port_for_pid(pid):
    r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    fallback = None
    for line in r.stdout.splitlines():
        low = line.lower()
        if "listening" not in low:
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
            if not port:
                continue
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
    if (CONFIG_FILE.exists() and not args.force):
        info("已存在配置：%s（要重写请加 --force）" % CONFIG_FILE)
    repo = args.repo or input("Codeberg 仓库地址（https://codeberg.org/你/oc-sync.git）：").strip()
    if not repo:
        die("仓库地址不能为空")
    envpw = os.environ.get("OC_SYNC_PASSPHRASE")
    if envpw:
        pw1 = pw2 = envpw
    else:
        pw1 = getpass.getpass("设置共享口令（5 台机器一致，建议 20+ 字符）：")
        pw2 = getpass.getpass("再输入一遍：")
    if pw1 != pw2:
        die("两次口令不一致")
    if not pw1:
        die("口令不能为空")
    machine = (args.machine or input("本机名字（默认 %s）：" % socket.gethostname())).strip()
    if not machine:
        machine = socket.gethostname()
    machine = re.sub(r"[^A-Za-z0-9_-]", "-", machine)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PASS_FILE.write_bytes(pw1.encode("utf-8"))
    save_config({"repo": repo, "machine": machine})
    ensure_repo()
    git_pull()
    info("初始化完成。机器名：%s" % machine)
    info("下一步：在任意一台机器先 push 一次全量，其余机器 pull。")


def cmd_push(args):
    ensure_repo()
    git_pull()
    heads = repo_heads()
    local = local_heads()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    machine = load_config()["machine"]
    passphrase = load_passphrase()
    bundle_dir().mkdir(parents=True, exist_ok=True)
    total = 0
    for agg, head in sorted(local.items()):
        after = heads.get(agg, -1)
        if head <= after:
            continue
        events = read_events(agg, after)
        if not events:
            continue
        lines = bytearray()
        for ev in events:
            lines += json.dumps(
                {"id": ev["id"], "aggregateID": ev["aggregateID"], "seq": ev["seq"],
                 "type": ev["type"], "data": ev["data"]},
                ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        name = bundle_name(stamp, machine, agg, events[0]["seq"], events[-1]["seq"])
        (bundle_dir() / name).write_bytes(encrypt_bytes(bytes(lines), passphrase))
        total += len(events)
    if total == 0:
        info("本机没有仓库缺少的新事件，无需推送。")
        return
    git_commit_push("sync %s %s: %d events" % (machine, stamp, total))
    info("已推送 %d 条新事件（%s）。" % (total, stamp))


def _replay_run(api, agg, run, directory):
    for i in range(0, len(run), CHUNK):
        chunk = run[i:i + CHUNK]
        api.post("/sync/replay", {"directory": directory or "", "events": chunk})


def cmd_pull(args):
    ensure_repo()
    git_pull()
    local = local_heads()
    needed = [b for b in scan_bundles() if b["max"] > local.get(b["agg"], -1)]
    if not needed:
        info("仓库里没有本机缺少的新事件。")
        return
    passphrase = load_passphrase()
    by_agg = {}
    for b in needed:
        plain = decrypt_bytes(b["path"].read_bytes(), passphrase)
        for line in plain.split(b"\n"):
            if not line.strip():
                continue
            ev = json.loads(line.decode("utf-8"))
            by_agg.setdefault(ev["aggregateID"], {})[ev["id"]] = ev
    if not by_agg:
        info("没有可导入的事件。")
        return
    port, pw, user = find_server()
    api = Api(port, pw, user)
    applied = 0
    sessions = 0
    for agg, evmap in by_agg.items():
        head = local.get(agg, -1)
        byseq = {}
        for ev in evmap.values():
            byseq.setdefault(ev["seq"], ev)
        run = []
        expect = head + 1
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
        _replay_run(api, agg, run, directory)
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
    ensure_repo()
    local = local_heads()
    heads = repo_heads()
    aggs = sorted(set(local) | set(heads))
    missing_local = 0
    missing_repo = 0
    for agg in aggs:
        lh = local.get(agg, -1)
        rh = heads.get(agg, -1)
        if rh > lh:
            missing_local += rh - lh
        if lh > rh:
            missing_repo += lh - rh
    info("本机会话数：%d，仓库已收录会话数：%d" % (len(local), len(heads)))
    info("待从仓库导入：%d 条事件" % missing_local)
    info("待推送到仓库：%d 条事件" % missing_repo)
    if not (REPO_DIR / ".git").exists():
        info("（仓库未初始化）")


def main():
    if os.name != "nt":
        die("当前只支持 Windows 桌面版 OpenCode")
    p = argparse.ArgumentParser(prog="oc-sync", description="OpenCode 聊天记录跨设备同步（私有 git + 端到端加密）")
    sub = p.add_subparsers(dest="cmd")
    pi = sub.add_parser("init", help="首次配置并克隆仓库")
    pi.add_argument("--repo", help="git 仓库地址")
    pi.add_argument("--machine", help="本机名字")
    pi.add_argument("--force", action="store_true", help="覆盖已有配置")
    pi.set_defaults(func=cmd_init)
    sub.add_parser("push", help="导出本机新事件并推送").set_defaults(func=cmd_push)
    sub.add_parser("pull", help="拉取并导入仓库新事件").set_defaults(func=cmd_pull)
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
