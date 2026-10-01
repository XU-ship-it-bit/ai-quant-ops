# -*- coding: utf-8 -*-
"""shadow_vps.py - VPS 影子三账本（每日 cron）：baseline / donchian30 / 1H。
数据 /root/quant_system/data/crypto；账本 /root/quant_system/team_data/shadow/。
与本地 16:20 批次同口径；本地电脑关机也照跑。幂等（同日不重复写）。
"""
import os, io, sys, json, math, datetime
import numpy as np
import pandas as pd

Q = "/root/quant_system"
DATA = os.path.join(Q, "data", "crypto")
SDIR = os.path.join(Q, "team_data", "shadow")
os.makedirs(SDIR, exist_ok=True)

RCFG = json.load(open(os.path.join(Q, "risk", "risk_config.json"), encoding="utf-8"))


def load(tag):
    df = pd.read_csv(os.path.join(DATA, tag + "_1d.csv"))
    tc = df.columns[0]
    df[tc] = pd.to_datetime(df[tc], utc=True)
    for c in ("open", "high", "low", "close"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_values(tc).reset_index(drop=True)


def load_bar(tag, bar):
    df = pd.read_csv(os.path.join(DATA, tag + "_" + bar + ".csv"))
    tc = df.columns[0]
    df[tc] = pd.to_datetime(df[tc], utc=True)
    for c in ("open", "high", "low", "close"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_values(tc).reset_index(drop=True)


def signal(df, factor):
    c, h, l = df["close"], df["high"], df["low"]
    if factor.startswith("donchian"):
        L = int(factor.replace("donchian", ""))
        ch, cl = h.rolling(L).max(), l.rolling(L).min()
        fac = (c - cl) / (ch - cl + 1e-12) * 2 - 1
    else:
        s, ll = factor.replace("ma_cross_", "").split("_")
        fac = c.rolling(int(s)).mean() / c.rolling(int(ll)).mean() - 1
    pos = pd.Series(0.0, index=df.index)
    pos[fac > 0] = 1.0
    return pos


def apply_risk(df, pos):
    """200MA 过滤 + 波动率目标 60%（clip 1.0）+ rebalance 0.10（与本地 shadow_trend 一致）"""
    c = df["close"]
    sma = c.rolling(int(RCFG["ma_filter"]["period"])).mean().values
    out = np.where(c.values < sma, 0.0, pos.values)
    lr = np.log(c / c.shift(1))
    rv = (lr.rolling(int(RCFG["vol_target"]["lookback_days"])).std()
          .ewm(span=int(RCFG["vol_target"]["ema_span"]), adjust=False).mean()
          * math.sqrt(RCFG["vol_target"]["annual_factor"])).values
    tv = RCFG["vol_target"]["target_annual_vol"]
    with np.errstate(divide="ignore", invalid="ignore"):
        t = tv / rv
    t = np.where(np.isnan(t), 0.0, t)
    t = np.clip(t, 0.0, 1.0)
    t = np.where(rv > RCFG["vol_target"]["extreme_vol_cutoff"], 0.0, t)
    raw = out * t
    held, res = 0.0, np.zeros(len(raw))
    thr = RCFG["vol_target"]["rebalance_threshold"]
    for i in range(len(raw)):
        if abs(raw[i] - held) > thr:
            held = raw[i]
        res[i] = 0.0 if out[i] == 0 else held
    return res


def daily_direction(df, fname):
    c, h, l = df["close"], df["high"], df["low"]
    if fname.startswith("donchian"):
        L = int(fname.replace("donchian", ""))
        ch, cl = h.rolling(L).max(), l.rolling(L).min()
        fac = (c - cl) / (ch - cl + 1e-12) * 2 - 1
    else:
        s, ll = fname.replace("ma_cross_", "").split("_")
        fac = c.rolling(int(s)).mean() / c.rolling(int(ll)).mean() - 1
    sma = c.rolling(200).mean()
    return ((fac > 0) & (c > sma)).astype(float).shift(1)


def h1_state(df_1):
    h1 = df_1["high"].rolling(80).max().shift(1)
    l1 = df_1["low"].rolling(80).min().shift(1)
    c1 = df_1["close"].values
    state = np.zeros(len(df_1))
    s = 0.0
    for i in range(len(df_1)):
        if np.isfinite(h1.iloc[i]) and c1[i] > h1.iloc[i]:
            s = 1.0
        elif np.isfinite(l1.iloc[i]) and c1[i] < l1.iloc[i]:
            s = 0.0
        state[i] = s
    return state


def ledger_write(name, recs):
    p = os.path.join(SDIR, name + ".jsonl")
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    seen = set()
    if os.path.exists(p):
        for ln in open(p, encoding="utf-8"):
            try:
                seen.add(json.loads(ln).get("date"))
            except Exception:
                pass
    if today not in seen:
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({"date": today, "strategies": recs}, ensure_ascii=False) + "\n")
        return True
    return False


def main():
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    print("shadow_vps run", today, flush=True)

    # 1. 基线（BTC donchian20 + ETH ma_cross_20_50，R3_longflat + 风控）
    recs = []
    for tag, fac in (("btc", "donchian20"), ("eth", "ma_cross_20_50")):
        df = load(tag)
        pos = apply_risk(df, signal(df, fac))
        recs.append({"sym": tag.upper(), "signal": "LONG" if float(pos[-1]) > 0 else "FLAT",
                     "target_pos": round(float(pos[-1]), 4), "close": float(df["close"].iloc[-1]),
                     "factor": fac, "role": "approved_pair"})
    ledger_write("trend_shadow", recs)
    print("baseline:", [(r["sym"], r["signal"], r["close"]) for r in recs], flush=True)

    # 2. donchian30 挑战者
    recs2 = []
    for tag in ("btc", "eth"):
        df = load(tag)
        pos = apply_risk(df, signal(df, "donchian30"))
        recs2.append({"sym": tag.upper(), "signal": "LONG" if float(pos[-1]) > 0 else "FLAT",
                      "target_pos": round(float(pos[-1]), 4), "close": float(df["close"].iloc[-1]),
                      "factor": "donchian30", "role": "challenger"})
    ledger_write("trend_shadow_challenger", recs2)
    print("challenger:", [(r["sym"], r["signal"]) for r in recs2], flush=True)

    # 3. 1H 挑战者（BTC/XRP 1H + ETH/SOL 日线）
    recs3 = []
    for tag, fac in (("btc", "donchian30"), ("xrp", "donchian20")):
        df_d = load(tag)
        df_1 = load_bar(tag, "1h")
        direction = float(daily_direction(df_d, fac).iloc[-1])
        direction = direction if np.isfinite(direction) else 0.0
        st = h1_state(df_1)
        sig = 1.0 if (direction > 0 and float(st[-1]) > 0) else 0.0
        recs3.append({"sym": tag.upper(), "signal": "LONG" if sig > 0 else "FLAT",
                      "close": float(df_1["close"].iloc[-1]), "factor": "1h_" + fac, "role": "challenger_1h"})
    for tag, fac in (("eth", "ma_cross_20_50"), ("sol", "donchian20")):
        df = load(tag)
        c = df["close"]
        sma = c.rolling(200).mean()
        if fac.startswith("donchian"):
            L = int(fac.replace("donchian", ""))
            facv = (c - c.rolling(L).min()) / (c.rolling(L).max() - c.rolling(L).min() + 1e-12) * 2 - 1
        else:
            s, l = fac.replace("ma_cross_", "").split("_")
            facv = c.rolling(int(s)).mean() / c.rolling(int(l)).mean() - 1
        long_now = bool((facv.iloc[-1] > 0) and (c.iloc[-1] > sma.iloc[-1]))
        recs3.append({"sym": tag.upper(), "signal": "LONG" if long_now else "FLAT",
                      "close": float(c.iloc[-1]), "factor": fac, "role": "challenger_1h"})
    ledger_write("trend_shadow_1h", recs3)
    print("1h:", [(r["sym"], r["signal"], r["close"]) for r in recs3], flush=True)
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
