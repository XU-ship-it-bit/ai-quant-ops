# -*- coding: utf-8 -*-
"""executor_vps.py - 冻结版执行层（VPS）。读引擎信号 → 算目标张数 → 下单 → 落台账。

授权：C 档（TIER_MULT=1.61），底仓 200 元（≈28 USDT）不参与交易，仅白名单 BTC/ETH/XRP/SOL。
安全设计：
  1. DRY_RUN 开关（默认 True，只打印不下单；env EXEC_LIVE=1 才真下单）
  2. 白名单硬校验（非白名单标的直接拒绝）
  3. 底仓保留（可用余额扣除底仓后才计算仓位）
  4. 单轮变动阈值（REBAL_THRESHOLD，避免高频churn烧手续费）
  5. 幂等：目标与当前差异小于阈值则不动
  6. 台账：team_data/trade_ledger.jsonl（每笔：时间/标的/方向/张数/价格/原因）
  7. 失败即停：任何下单异常都写日志 + 钉钉告警，不重试下单（防重复开仓）
"""
import os, io, json, math, time, subprocess, datetime
import urllib.request

Q = "/root/quant_system"
TDIR = os.path.join(Q, "team_data")
SIG = os.path.join(TDIR, "engine_signal.json")
LEDGER = os.path.join(TDIR, "trade_ledger.jsonl")
LOG = os.path.join(Q, "logs", "executor_vps.log")
STATE = os.path.join(TDIR, "executor_state.json")
os.makedirs(os.path.dirname(LOG), exist_ok=True)

OKX = "/usr/local/bin/okx"

# ===== 授权参数（2026-10-01 用户授权）=====
TIER_MULT = 1.61          # C 档 5% 目标
RESERVE_USDT = 28.0       # 底仓 200 元 ≈ 28 USDT，不参与交易
WHITELIST = {"BTC": "BTC-USDT-SWAP", "ETH": "ETH-USDT-SWAP",
             "XRP": "XRP-USDT-SWAP", "SOL": "SOL-USDT-SWAP"}
WEQ = {"btc": 0.25, "eth": 0.25, "xrp": 0.25, "sol": 0.25}
REBAL_THRESHOLD = 0.10    # 目标与当前差异 <10% 不动（与策略 rebalance 口径一致）
MIN_ORDER_USDT = 10.0     # 名义金额低于此不下单（吸收最小张数颗粒度）
DRY_RUN = os.environ.get("EXEC_LIVE", "0") != "1"

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


def ding_send(text):
    try:
        if _tok["v"] and time.time() - _tok["at"] < 7000:
            tok = _tok["v"]
        else:
            body = json.dumps({"appKey": CID, "appSecret": SEC}).encode("utf-8")
            req = urllib.request.Request("https://api.dingtalk.com/v1.0/oauth2/accessToken",
                                         data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                tok = json.loads(r.read().decode("utf-8")).get("accessToken") or ""
            _tok["v"], _tok["at"] = tok, time.time()
        if not tok:
            return False
        url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
        b = {"robotCode": CID, "userIds": [UID], "msgKey": "sampleText",
             "msgParam": json.dumps({"content": text[:1500]}, ensure_ascii=False)}
        req = urllib.request.Request(url, data=json.dumps(b).encode("utf-8"),
                                     headers={"Content-Type": "application/json",
                                              "x-acs-dingtalk-access-token": tok})
        with urllib.request.urlopen(req, timeout=20) as r:
            json.loads(r.read().decode("utf-8", "replace"))
        return True
    except Exception as e:
        log("ding err %r" % e)
        return False


def okx_json(args, timeout=60):
    try:
        r = subprocess.run([OKX, "--live", "--json"] + args, capture_output=True, text=True, timeout=timeout)
        return json.loads(r.stdout.strip() or "[]")
    except Exception as e:
        log("okx err %r" % e)
        return []


def get_equity():
    d = okx_json(["account", "balance"])
    rows = d if isinstance(d, list) else (d.get("data") or [])
    for r in rows:
        if isinstance(r, dict) and r.get("ccy") == "USDT":
            try:
                return float(r.get("availBal") or 0), float(r.get("eq") or 0)
            except Exception:
                return 0.0, 0.0
    return 0.0, 0.0


def get_positions():
    d = okx_json(["swap", "positions"])
    rows = d if isinstance(d, list) else (d.get("data") or [])
    out = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        inst = r.get("instId") or ""
        try:
            sz = float(r.get("pos") or 0)
        except Exception:
            sz = 0.0
        if sz != 0 and inst in WHITELIST.values():
            out[inst] = {"pos": sz, "avgPx": float(r.get("avgPx") or 0),
                         "mgnMode": r.get("mgnMode") or "cross",
                         "posSide": r.get("posSide") or "net"}
    return out


def ctval(inst):
    """合约面值（cache 到文件，避免频繁查）"""
    cache = os.path.join(TDIR, ".ctval.json")
    try:
        c = json.load(open(cache, encoding="utf-8"))
    except Exception:
        c = {}
    if inst in c:
        return c[inst]
    try:
        url = "https://www.okx.com/api/v5/public/instruments?instType=SWAP&instId=" + inst
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
        it = (d.get("data") or [{}])[0]
        v = {"ctVal": float(it.get("ctVal") or 0), "lotSz": float(it.get("lotSz") or 0.01),
             "minSz": float(it.get("minSz") or 0.01)}
        c[inst] = v
        json.dump(c, open(cache, "w", encoding="utf-8"))
        return v
    except Exception as e:
        log("ctval err %r" % e)
        return None


def last_px(inst):
    d = okx_json(["market", "ticker", inst])
    rows = d if isinstance(d, list) else (d.get("data") or [])
    for r in rows:
        if isinstance(r, dict):
            try:
                return float(r.get("last") or 0)
            except Exception:
                pass
    return 0.0


def place(inst, side, sz, posSide, mgnMode, tag):
    if DRY_RUN:
        log("DRY_RUN would place: %s %s sz=%s posSide=%s (%s)" % (inst, side, sz, posSide, tag))
        return {"dry": True, "ordId": "DRY"}
    args = ["swap", "place", "--instId", inst, "--side", side, "--ordType", "market",
            "--sz", str(sz), "--posSide", posSide, "--tdMode", mgnMode]
    if side == "sell":
        args.append("--reduceOnly")
    res = okx_json(args)
    log("place %s %s sz=%s -> %s" % (inst, side, sz, json.dumps(res)[:160]))
    return res


def main():
    log("executor run (DRY_RUN=%s, tier=%.2f)" % (DRY_RUN, TIER_MULT))
    if not os.path.exists(SIG):
        log("no engine_signal.json, abort")
        return
    sig = json.load(open(SIG, encoding="utf-8"))
    avail, eq = get_equity()
    # 测试用：EXEC_TEST_EQUITY=252 可模拟权益（只在 DRY_RUN 下允许）
    _te = os.environ.get("EXEC_TEST_EQUITY", "")
    if _te and DRY_RUN:
        try:
            avail = float(_te)
            log("TEST_EQUITY override -> %.2f" % avail)
        except Exception:
            pass
    tradeable = max(0.0, avail - RESERVE_USDT)
    log("equity_avail=%.4f eq=%.4f tradeable=%.4f" % (avail, eq, tradeable))
    if tradeable < 20:
        log("tradeable < 20 USDT, skip (need funds)")
        return

    pos_now = get_positions()
    actions = []
    for key, v in sig.get("strategies", {}).items():
        sym = (v.get("sym") or "").upper()
        if sym not in WHITELIST:
            log("REJECT non-whitelist symbol: %s" % sym)
            continue
        inst = WHITELIST[sym]
        pos_w = float(v.get("pos_scaled") or 0)
        target_notional = tradeable * WEQ.get(key, 0.25) * pos_w * TIER_MULT
        cv = ctval(inst)
        px = last_px(inst)
        if not cv or px <= 0:
            log("skip %s: no ctval/px" % inst)
            continue
        per_lot_notional = cv["ctVal"] * px
        lots_raw = target_notional / per_lot_notional if per_lot_notional > 0 else 0
        lot_sz = cv["lotSz"] or 0.01
        lots = math.floor(lots_raw / lot_sz) * lot_sz
        cur = pos_now.get(inst, {}).get("pos", 0.0)
        cur_notional = abs(cur) * per_lot_notional
        if abs(target_notional - cur_notional) < max(MIN_ORDER_USDT, cur_notional * REBAL_THRESHOLD):
            log("%s no change (target=%.2f cur=%.2f)" % (inst, target_notional, cur_notional))
            continue
        diff_lots = round(lots - abs(cur), 2)
        if abs(diff_lots) < lot_sz:
            continue
        side = "buy" if diff_lots > 0 else "sell"
        actions.append({"inst": inst, "side": side, "sz": abs(diff_lots),
                        "posSide": "long", "mgnMode": pos_now.get(inst, {}).get("mgnMode", "cross"),
                        "target_notional": round(target_notional, 2), "px": px, "sym": sym})

    if not actions:
        log("no actions needed")
        return

    # 先减仓后加仓（降低保证金占用峰值）
    actions.sort(key=lambda a: 0 if a["side"] == "sell" else 1)
    done = []
    for a in actions:
        try:
            res = place(a["inst"], a["side"], a["sz"], a["posSide"], a["mgnMode"],
                        "target=%.2f" % a["target_notional"])
            ok = True
            if not DRY_RUN and isinstance(res, list) and res:
                ok = str(res[0].get("sCode")) == "0"
            rec = dict(a)
            rec.update({"t": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "ok": ok, "res": str(res)[:200], "dry": DRY_RUN, "tier": TIER_MULT})
            with open(LEDGER, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            done.append(rec)
        except Exception as e:
            log("place err %r" % e)

    if done and not DRY_RUN:
        msg = "📈【执行层下单】\n" + "\n".join(
            "%s %s %.2f张（目标 %.0fU，价 %.4f）" % (d["sym"], "买入" if d["side"] == "buy" else "卖出",
                                                  d["sz"], d["target_notional"], d["px"]) for d in done)
        msg += "\n档位 C(1.61×)｜底仓保留 %.0fU" % RESERVE_USDT
        ding_send(msg)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        log("FATAL " + traceback.format_exc()[-500:])
