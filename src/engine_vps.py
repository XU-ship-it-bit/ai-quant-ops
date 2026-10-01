# -*- coding: utf-8 -*-
"""engine_vps.py - 冻结版 4H 仓位引擎（VPS · 只算仓不下单）。

口径（与 ledger_frozen.py 冻结版完全一致）：
  BTC 4H（日线 donchian30 方向）+ XRP 4H（日线 donchian20 方向）+ ETH 日线（ma_cross_20_50）+ SOL 日线（donchian20）
  标的级 vol60% + rebalance 阈值 0.10 + clip_max 3.0 + cap120%（比例缩仓）+ 滞后 1 天。
输出：
  /root/quant_system/team_data/engine_signal.json（最新目标仓位）
  /root/quant_system/team_data/engine_state.json（rebalance 持有的仓位）
  /root/quant_system/logs/engine_vps.log
  状态翻转（入场/离场）→ 钉钉单聊告警。
红线：本脚本只读行情 + 写本地文件 + 发告警，绝不下单。
"""
import os, io, sys, json, math, datetime, time
import urllib.request
import numpy as np
import pandas as pd

Q = "/root/quant_system"
DATA = os.path.join(Q, "data", "crypto")
TDIR = os.path.join(Q, "team_data")
os.makedirs(TDIR, exist_ok=True)
SIG = os.path.join(TDIR, "engine_signal.json")
STATE = os.path.join(TDIR, "engine_state.json")
LOG = os.path.join(Q, "logs", "engine_vps.log")
os.makedirs(os.path.dirname(LOG), exist_ok=True)

RCFG = json.load(open(os.path.join(Q, "risk", "risk_config.json"), encoding="utf-8"))
BASE_TV = 0.60
CLIP_MAX = 3.0
REBAL = 0.10
CAP = 1.2
WEQ = {"btc": 0.25, "eth": 0.25, "xrp": 0.25, "sol": 0.25}

# 钉钉（只发告警）
CID = "YOUR_DINGTALK_ROBOT_CODE"
SEC = "YOUR_DINGTALK_APP_SECRET"
UID = "YOUR_DINGTALK_USER_ID"
_tok = {"v": "", "at": 0.0}


def log(m):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S") + " " + m + "\n")
    except Exception:
        pass


def ding_token():
    if _tok["v"] and time.time() - _tok["at"] < 7000:
        return _tok["v"]
    try:
        body = json.dumps({"appKey": CID, "appSecret": SEC}).encode("utf-8")
        req = urllib.request.Request("https://api.dingtalk.com/v1.0/oauth2/accessToken",
                                     data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            j = json.loads(r.read().decode("utf-8"))
        _tok["v"] = str(j.get("accessToken") or "")
        _tok["at"] = time.time()
        return _tok["v"]
    except Exception as e:
        log("ding token err %r" % e)
        return ""


def ding_send(text):
    try:
        tok = ding_token()
        if not tok:
            return False
        url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
        body = {"robotCode": CID, "userIds": [UID],
                "msgKey": "sampleText", "msgParam": json.dumps({"content": text[:1600]}, ensure_ascii=False)}
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json",
                                              "x-acs-dingtalk-access-token": tok})
        with urllib.request.urlopen(req, timeout=20) as r:
            j = json.loads(r.read().decode("utf-8", "replace"))
        log("ding send ok: %s" % str(j)[:120])
        return True
    except Exception as e:
        log("ding send err %r" % e)
        return False


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


def vol_size(df):
    c = df["close"]
    lr = np.log(c / c.shift(1))
    rv = (lr.rolling(20).std().ewm(span=10, adjust=False).mean() * math.sqrt(365)).values
    with np.errstate(divide="ignore", invalid="ignore"):
        t = BASE_TV / rv
    t = np.where(np.isnan(t), 0.0, t)
    t = np.clip(t, 0.0, CLIP_MAX)
    t = np.where(rv > 0.80, 0.0, t)
    return t


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


def h4_state(df_4):
    h4 = df_4["high"].rolling(20).max().shift(1)
    l4 = df_4["low"].rolling(20).min().shift(1)
    c4 = df_4["close"].values
    state = np.zeros(len(df_4))
    s = 0.0
    for i in range(len(df_4)):
        if np.isfinite(h4.iloc[i]) and c4[i] > h4.iloc[i]:
            s = 1.0
        elif np.isfinite(l4.iloc[i]) and c4[i] < l4.iloc[i]:
            s = 0.0
        state[i] = s
    return state


def load_state():
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except Exception:
        return {"held": {}, "prev_sig": {}}


def save_state(st):
    json.dump(st, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def main():
    now = datetime.datetime.utcnow()
    st = load_state()
    held = st.get("held", {})
    prev_sig = st.get("prev_sig", {})

    out = {}
    praw = 0.0
    alerts = []

    # ---- 4H 腿：BTC donchian30 / XRP donchian20 ----
    for tag, fname in (("btc", "donchian30"), ("xrp", "donchian20")):
        df_d = load(tag)
        df_4 = load_bar(tag, "4h")
        ddir = daily_direction(df_d, fname).values
        direction = float(ddir[-1]) if np.isfinite(ddir[-1]) else 0.0
        ts = vol_size(df_d)
        tsize = float(ts[-2]) if len(ts) > 1 and np.isfinite(ts[-2]) else 0.0   # 滞后 1 天
        state_arr = h4_state(df_4)
        st_now = float(state_arr[-1])
        sig = 1.0 if (direction > 0 and st_now > 0) else 0.0
        raw = sig * tsize
        held_v = float(held.get(tag, 0.0))
        if abs(raw - held_v) > REBAL:
            held_v = raw
        pos = 0.0 if sig == 0 else held_v
        held[tag] = held_v
        out[tag] = {"sym": tag.upper(), "leg": "4h", "factor": fname,
                    "direction": direction, "h4_state": st_now, "vol_size": round(tsize, 4),
                    "signal": "LONG" if sig > 0 else "FLAT", "pos": round(pos, 4),
                    "close": float(df_4["close"].iloc[-1])}
        praw += WEQ[tag] * pos
        ps = prev_sig.get(tag, 0.0)
        if sig != ps:
            alerts.append("🔔 %s 4H %s（%.4f → %.4f）" % (tag.upper(), "入场" if sig > 0 else "离场", ps, pos))
            prev_sig[tag] = sig

    # ---- 日线腿：ETH ma_cross / SOL donchian20 ----
    for tag, fname in (("eth", "ma_cross_20_50"), ("sol", "donchian20")):
        df = load(tag)
        c = df["close"]
        sma = c.rolling(200).mean()
        if fname.startswith("donchian"):
            L = int(fname.replace("donchian", ""))
            fac = (c - c.rolling(L).min()) / (c.rolling(L).max() - c.rolling(L).min() + 1e-12) * 2 - 1
        else:
            s, l = fname.replace("ma_cross_", "").split("_")
            fac = c.rolling(int(s)).mean() / c.rolling(int(l)).mean() - 1
        long_now = 1.0 if (fac.iloc[-1] > 0 and c.iloc[-1] > sma.iloc[-1]) else 0.0
        ts = vol_size(df)
        tsize = float(ts[-1]) if np.isfinite(ts[-1]) else 0.0
        raw = long_now * tsize
        held_v = float(held.get(tag, 0.0))
        if abs(raw - held_v) > REBAL:
            held_v = raw
        pos = 0.0 if long_now == 0 else held_v
        held[tag] = held_v
        out[tag] = {"sym": tag.upper(), "leg": "daily", "factor": fname,
                    "signal": "LONG" if long_now > 0 else "FLAT", "pos": round(pos, 4),
                    "close": float(c.iloc[-1])}
        praw += WEQ[tag] * pos
        ps = prev_sig.get(tag, 0.0)
        if long_now != ps:
            alerts.append("🔔 %s 日线 %s（%.4f → %.4f）" % (tag.upper(), "入场" if long_now > 0 else "离场", ps, pos))
            prev_sig[tag] = long_now

    # cap120% 比例缩仓（口径 A）
    scale = min(1.0, CAP / praw) if praw > 1e-9 else 1.0
    for tag in out:
        out[tag]["pos_scaled"] = round(out[tag]["pos"] * scale, 4)

    sig_doc = {"t": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
               "cap_scale": round(scale, 4),
               "total_exposure": round(praw, 4),
               "strategies": out}
    json.dump(sig_doc, open(SIG, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    st["held"] = held
    st["prev_sig"] = prev_sig
    save_state(st)

    log("engine tick: scale=%.2f exposure=%.2f %s" % (scale, praw,
        " ".join("%s:%s/%.2f" % (k, v["signal"], v["pos_scaled"]) for k, v in out.items())))
    if alerts:
        msg = "⚡【4H 引擎状态翻转】\n" + "\n".join(alerts) + "\n\n组合总敞口 %.2f（cap %.2f）" % (praw * scale, CAP)
        ding_send(msg)
        log("alerts sent: %s" % "; ".join(alerts))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        log("FATAL " + traceback.format_exc()[-500:])
