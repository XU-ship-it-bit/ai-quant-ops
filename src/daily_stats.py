# -*- coding: utf-8 -*-
"""daily_stats.py - 每日金额统计（VPS）：入账 / 亏损 / 手续费 / 净额 / 期末权益。

数据源：OKX 账单（pnl、fee）+ 账户权益。
输出：team_data/daily_pnl.jsonl（逐日明细）+ team_data/daily_pnl.md（人看的表）
时区口径：按北京时间（UTC+8）分日。
用法：python daily_stats.py [天数，默认7]
"""
import os, io, json, subprocess, datetime, sys

Q = "/root/quant_system"
TDIR = os.path.join(Q, "team_data")
JL = os.path.join(TDIR, "daily_pnl.jsonl")
MD = os.path.join(TDIR, "daily_pnl.md")
LOG = os.path.join(Q, "logs", "daily_stats.log")
os.makedirs(os.path.dirname(LOG), exist_ok=True)
OKX = "/usr/local/bin/okx"
BJ = datetime.timezone(datetime.timedelta(hours=8))


def log(m):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(datetime.datetime.now(BJ).strftime("%Y-%m-%d %H:%M:%S") + " " + m + "\n")
    except Exception:
        pass


def okx_json(args, timeout=60):
    try:
        r = subprocess.run([OKX, "--live", "--json"] + args, capture_output=True, text=True, timeout=timeout)
        return json.loads(r.stdout.strip() or "[]")
    except Exception as e:
        log("okx err %r" % e)
        return []


def equity_now():
    d = okx_json(["account", "balance"])
    rows = d if isinstance(d, list) else (d.get("data") or [])
    tot = 0.0
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            if r.get("ccy") == "USDT":
                return float(r.get("eq") or r.get("availBal") or 0)
        except Exception:
            pass
    return tot


def fetch_bills(days):
    """拉账单（SWAP + 全部），返回按北京时间分日的聚合"""
    out = {}
    for args in (["account", "bills", "--limit", "100"],):
        d = okx_json(args)
        rows = d if isinstance(d, list) else (d.get("data") or [])
        for r in rows:
            if not isinstance(r, dict):
                continue
            try:
                ts = int(r.get("ts") or 0)
            except Exception:
                continue
            day = datetime.datetime.fromtimestamp(ts / 1000.0, BJ).strftime("%Y-%m-%d")
            try:
                pnl = float(r.get("pnl") or 0)
            except Exception:
                pnl = 0.0
            try:
                fee = float(r.get("fee") or 0)
            except Exception:
                fee = 0.0
            a = out.setdefault(day, {"in": 0.0, "out": 0.0, "fee": 0.0, "n": 0})
            if pnl > 0:
                a["in"] += pnl
            elif pnl < 0:
                a["out"] += pnl
            a["fee"] += fee
            a["n"] += 1
    return out


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    eq = equity_now()
    bills = fetch_bills(days)
    today = datetime.datetime.now(BJ).strftime("%Y-%m-%d")

    # 读已有逐日记录
    recs = {}
    if os.path.exists(JL):
        for ln in io.open(JL, encoding="utf-8"):
            try:
                j = json.loads(ln)
                if j.get("date"):
                    recs[j["date"]] = j
            except Exception:
                pass

    # 更新今天（含权益快照）
    a = bills.get(today, {"in": 0.0, "out": 0.0, "fee": 0.0, "n": 0})
    cur = recs.get(today, {})
    cur.update({"date": today, "in": round(a["in"], 4), "out": round(a["out"], 4),
                "fee": round(a["fee"], 6), "net": round(a["in"] + a["out"] + a["fee"], 4),
                "trades": a["n"], "equity": round(eq, 4),
                "updated": datetime.datetime.now(BJ).strftime("%Y-%m-%d %H:%M")})
    recs[today] = cur
    # 补历史日（若账单里有但账本没有）
    for day, v in bills.items():
        if day not in recs:
            recs[day] = {"date": day, "in": round(v["in"], 4), "out": round(v["out"], 4),
                         "fee": round(v["fee"], 6), "net": round(v["in"] + v["out"] + v["fee"], 4),
                         "trades": v["n"], "equity": None, "updated": "-"}

    keys = sorted(recs)[-max(days, 14):]
    with io.open(JL, "w", encoding="utf-8", newline="\n") as f:
        for k in keys:
            f.write(json.dumps(recs[k], ensure_ascii=False) + "\n")

    # 生成 markdown 表
    lines = ["# 每日金额统计（北京时间）", "",
             "| 日期 | 入账 | 亏损 | 手续费 | 净额 | 成交笔数 | 期末权益 |",
             "|---|---|---|---|---|---|---|"]
    tot = {"in": 0.0, "out": 0.0, "fee": 0.0, "net": 0.0}
    for k in keys:
        r = recs[k]
        tot["in"] += r["in"]; tot["out"] += r["out"]; tot["fee"] += r["fee"]; tot["net"] += r["net"]
        lines.append("| %s | %+.3f | %.3f | %.4f | **%+.3f** | %d | %s |" % (
            k, r["in"], r["out"], r["fee"], r["net"], r["trades"],
            ("%.2f" % r["equity"]) if r.get("equity") is not None else "-"))
    lines.append("| **合计** | **%+.3f** | **%.3f** | **%.4f** | **%+.3f** | | |" % (
        tot["in"], tot["out"], tot["fee"], tot["net"]))
    lines += ["", "> 入账=当日盈利单合计；亏损=当日亏损单合计（负数）；净额=入账+亏损+手续费。",
              "> 单位：USDT。数据源：OKX 账单 + 账户权益。"]
    io.open(MD, "w", encoding="utf-8", newline="\n").write("\n".join(lines) + "\n")
    log("stats updated: equity=%.4f today in=%.4f out=%.4f" % (eq, cur["in"], cur["out"]))
    print("OK equity=%.4f 今日入账=%.4f 今日亏损=%.4f 净=%.4f" % (eq, cur["in"], cur["out"], cur["net"]))


if __name__ == "__main__":
    main()
