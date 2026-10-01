# -*- coding: utf-8 -*-
"""singleton.py - 单实例守卫（2026-10-01 立）。

背景：守护脚本只"写"锁不"查"锁，反复重启会堆出重复进程（本次清查出 4 组重复）。
用法：
    from singleton import ensure_single
    ensure_single("pulse_bills", Q)   # 已有存活实例则直接 os._exit(0)
"""
import os
import sys


def _pid_alive(pid):
    try:
        pid = int(pid)
    except Exception:
        return False
    try:
        import subprocess
        r = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid],
                           capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return str(pid) in r.stdout.decode("gbk", "replace")
    except Exception:
        return False


def ensure_single(name, qdir):
    """已有同名单实例存活 → 立即退出；否则写入自己的 PID。"""
    lock = os.path.join(qdir, "team_data", ".%s.lock" % name)
    try:
        if os.path.exists(lock):
            old = open(lock, encoding="utf-8", errors="replace").read().strip()
            if old.isdigit() and int(old) != os.getpid() and _pid_alive(old):
                sys.stderr.write("[singleton] %s already running (pid=%s), exit\n" % (name, old))
                os._exit(0)
    except Exception:
        pass
    try:
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        with open(lock, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass
