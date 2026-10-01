# -*- coding: utf-8 -*-
"""sync_vps_ledgers.py - 本地从 VPS 单向同步账本（2026-10-01 立，单一数据源）。

背景：本地影子任务与 VPS 重复跑，且本地数据更新已停 → 会算出陈旧信号。
现在 VPS 为唯一权威源；本地只做"拉取+展示"。
规则：按 date 合并，**VPS 版本优先**（本地仅补 VPS 没有的更早历史）。
顺带把引擎信号也拉下来，供本地看板查看。
"""
import os, io, json, subprocess, sys

KEY = r"C:\path\to\quant_system\_vps\id_ed25519"
HOST = "root@YOUR_VPS_HOST"
Q = r"C:\path\to\quant_system"
LOCAL_DIR = os.path.join(Q, "team_data", "shadow")
TMP = os.path.join(Q, "team_data", "_vps_pull")
LEDGERS = ["trend_shadow.jsonl", "trend_shadow_challenger.jsonl", "trend_shadow_1h.jsonl"]
EXTRA = [("team_data/engine_signal.json", "engine_signal.json"),
         ("team_data/discipline_state.json", "discipline_state.json")]


def sh(cmd, timeout=180):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        print("CMD ERR", str(e)[:120])
        return None


def main():
    os.makedirs(TMP, exist_ok=True)
    os.makedirs(LOCAL_DIR, exist_ok=True)
    # 1. 拉 VPS 账本
    for f in LEDGERS:
        r = sh('scp -i "%s" -o StrictHostKeyChecking=no %s:/root/quant_system/team_data/shadow/%s "%s\\%s"'
               % (KEY, HOST, f, TMP, f))
        if r is None or r.returncode != 0:
            print("拉取失败:", f)
    # 2. 按 date 合并（VPS 优先）
    for f in LEDGERS:
        vps_p = os.path.join(TMP, f)
        loc_p = os.path.join(LOCAL_DIR, f)
        vps_rows = {}
        if os.path.exists(vps_p):
            for ln in io.open(vps_p, encoding="utf-8"):
                try:
                    j = json.loads(ln)
                    if j.get("date"):
                        vps_rows[j["date"]] = j
                except Exception:
                    pass
        loc_rows = {}
        if os.path.exists(loc_p):
            for ln in io.open(loc_p, encoding="utf-8"):
                try:
                    j = json.loads(ln)
                    if j.get("date"):
                        loc_rows[j["date"]] = j
                except Exception:
                    pass
        merged = dict(loc_rows)
        merged.update(vps_rows)          # VPS 覆盖同名日期
        out = [merged[d] for d in sorted(merged)]
        with io.open(loc_p, "w", encoding="utf-8", newline="\n") as fh:
            for r in out:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        print("%s: 本地 %d + VPS %d -> 合并 %d 行（%s..%s）"
              % (f, len(loc_rows), len(vps_rows), len(out),
                 out[0]["date"] if out else "-", out[-1]["date"] if out else "-"))
    # 3. 拉引擎信号等
    for remote, local in EXTRA:
        sh('scp -i "%s" -o StrictHostKeyChecking=no %s:/root/quant_system/%s "%s\\%s"'
           % (KEY, HOST, remote, os.path.join(Q, "team_data"), local))
    print("DONE")


if __name__ == "__main__":
    main()
