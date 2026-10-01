# -*- coding: utf-8 -*-
"""discipline_guard.py v3 - 纪律锁（VPS，每 2 分钟 cron）。

分类（OKX 官方现货表为准，不会认不出）：
  美股 = OKX 现货 X 前缀（XSNDK/XSOXS 等）对应的 SWAP，自动拉全，绝不动；
  正规加密 = BTC / ETH / XRP / SOL；
  垃圾币 = 其余任何加密 SWAP 持仓 → 立即强平 + 钉钉告警 + 冷静期。
冷静期（快速递进）：第 1 次 3 天 → 第 2 次 7 天 → 第 3 次起 14 天。
冷静期内锁死：期间再开垃圾币继续强平；7 天无违规复位。
安全兜底：分类表拉不到（API 失败且无缓存）→ 只告警不强平，绝不错杀美股。
硬规则：冷静期不可解锁——上头了求解锁也不解。
"""
import os, io, sys, json, subprocess, datetime, time
import urllib.request

Q = "/root/quant_system"
TDIR = os.path.join(Q, "team_data")
STATE = os.path.join(TDIR, "discipline_state.json")
CACHE = os.path.join(TDIR, "market_classify_cache.json")
LOG = os.path.join(Q, "logs", "discipline_guard.log")
os.makedirs(os.path.dirname(LOG), exist_ok=True)

OKX = "/usr/local/bin/okx"

ALLOWED_CRYPTO = {"BTC", "ETH", "XRP", "SOL"}
COOLDOWN_DAYS = {1: 3, 2: 7}     # 第 1 次 3 天，第 2 次 7 天，第 3 次起 14 天
COOLDOWN_MAX = 14

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
            json.loads(r.read().decode("utf-8", "replace"))
        return True
    except Exception as e:
        log("ding send err %r" % e)
        return False


def okx_json(args, timeout=60):
    cmd = [OKX, "--live", "--json"] + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return json.loads(r.stdout.strip() or "[]")
    except Exception as e:
        log("okx err %r" % e)
        return []


def load_markets():
    """美股集合 + 加密集合（OKX 现货官方分类：X 前缀 = 美股，非 X = 加密）。带缓存。"""
    try:
        data = okx_json(["market", "tickers", "SPOT"])
        rows = data if isinstance(data, list) else (data.get("data") or [])
        us, crypto = set(), set()
        for r in rows:
            if not isinstance(r, dict):
                continue
            inst = r.get("instId") or ""
            if inst.startswith("X") and inst.endswith("-USDT"):
                us.add(inst[1:-5])
            elif inst.endswith("-USDT"):
                crypto.add(inst[:-5])
        if us:
            json.dump({"us": sorted(us), "crypto": sorted(crypto)}, open(CACHE, "w"))
            return us, crypto
    except Exception as e:
        log("load markets err %r" % e)
    try:
        c = json.load(open(CACHE, encoding="utf-8"))
        return set(c.get("us", [])), set(c.get("crypto", []))
    except Exception:
        return set(), set()


def get_positions():
    data = okx_json(["swap", "positions"])
    rows = data if isinstance(data, list) else (data.get("data") or [])
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        sz = float(r.get("pos") or 0)
        if sz == 0:
            continue
        out.append({"instId": r.get("instId"), "pos": sz, "avgPx": r.get("avgPx"),
                    "mgnMode": r.get("mgnMode"), "side": r.get("posSide")})
    return out


def classify(inst, us, crypto):
    ticker = (inst or "").split("-")[0].upper()
    if ticker in ALLOWED_CRYPTO:
        return "allowed"
    if ticker in us:
        return "allowed"       # 美股：绝不动
    if ticker in crypto:
        return "junk"          # 加密且非 BTC/ETH/XRP/SOL → 垃圾币
    return "unknown"           # 两个集合都没有 → 只告警


def force_close(pos):
    side = "sell" if float(pos["pos"]) > 0 else "buy"
    sz = abs(float(pos["pos"]))
    args = ["swap", "place", "--instId", pos["instId"], "--side", side,
            "--ordType", "market", "--sz", str(sz), "--posSide", pos.get("side") or "long",
            "--reduceOnly", "--tdMode", pos.get("mgnMode") or "cross"]
    res = okx_json(args)
    log("force close %s %s x%s -> %s" % (pos["instId"], side, sz, json.dumps(res)[:150]))
    return res


def load_state():
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except Exception:
        return {"violations": 0, "cooldown_until": 0, "last_violation": 0, "closed_log": []}


def save_state(st):
    json.dump(st, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def main():
    st = load_state()
    now = time.time()
    us, crypto = load_markets()
    if not us:
        log("WARN: market classify empty, skip kill (safe mode)")
        return
    pos_list = get_positions()

    junk = []
    unknown = []
    for p in pos_list:
        cls = classify(p["instId"], us, crypto)
        if cls == "junk":
            junk.append(p)
        elif cls == "unknown":
            unknown.append(p)

    if unknown:
        msg = ("⚠️【纪律锁 · 无法分类】\n以下持仓不在美股也不在加密现货表，只告警不强平：\n%s\n请手动确认。"
               % "\n".join("- %s x%s" % (p["instId"], p["pos"]) for p in unknown))
        ding_send(msg)
        log("unknown: %s" % "; ".join(p["instId"] for p in unknown))

    if not junk:
        if st.get("violations", 0) > 0 and now - st.get("last_violation", 0) > 7 * 86400:
            st["violations"] = 0
            save_state(st)
            log("7 天无违规，复位")
        return

    # 有垃圾币 → 立即强平 + 冷静期
    st["violations"] = int(st.get("violations", 0)) + 1
    v = st["violations"]
    days = COOLDOWN_DAYS.get(v, COOLDOWN_MAX)
    st["cooldown_until"] = now + days * 86400
    st["last_violation"] = now

    closed = []
    for p in junk:
        try:
            force_close(p)
            closed.append("%s x%s @%s" % (p["instId"], p["pos"], p["avgPx"]))
            st.setdefault("closed_log", []).append(
                "%s | 强平 %s x%s" % (datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M"), p["instId"], p["pos"]))
        except Exception as e:
            log("force close err %r" % e)
    save_state(st)

    msg = ("🚨【纪律锁 · 第 %d 次违规】\n垃圾币已强平：\n%s\n\n"
           "合约冷静期 %d 天：期间任何新开垃圾币会被继续强平，冷静期不可解锁。\n"
           "白名单：美股（全部）+ BTC/ETH/XRP/SOL。玩正规市场。"
           % (v, "\n".join(closed) if closed else "（强平失败，手动处理！）", days))
    ding_send(msg)
    log("violation #%d, cooldown %d days, closed: %s" % (v, days, "; ".join(closed) if closed else "none"))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        log("FATAL " + traceback.format_exc()[-400:])
