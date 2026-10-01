# -*- coding: utf-8 -*-
"""health_patrol.py - VPS 健康巡检（每 30 分钟）。

检查项：① 加密数据新鲜度 ② 4H 引擎产出新鲜度 ③ 影子账本新鲜度 ④ 纪律锁状态
异常 → 钉钉告警（同类问题 6 小时内只报一次，防刷屏）。
"""
import os, io, json, time, datetime
import urllib.request

Q = "/root/quant_system"
TDIR = os.path.join(Q, "team_data")
DATA = os.path.join(Q, "data", "crypto")
LOG = os.path.join(Q, "logs", "health_patrol.log")
STATE = os.path.join(TDIR, ".health_patrol_state.json")
os.makedirs(os.path.dirname(LOG), exist_ok=True)

CID = "YOUR_DINGTALK_ROBOT_CODE"
SEC = "YOUR_DINGTALK_APP_SECRET"
UID = "YOUR_DINGTALK_USER_ID"
REPORT_COOLDOWN = 6 * 3600
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
        log("token err %r" % e)
        return ""


def ding_send(text):
    try:
        tok = ding_token()
        if not tok:
            return False
        url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
        body = {"robotCode": CID, "userIds": [UID], "msgKey": "sampleText",
                "msgParam": json.dumps({"content": text[:1600]}, ensure_ascii=False)}
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json",
                                              "x-acs-dingtalk-access-token": tok})
        with urllib.request.urlopen(req, timeout=20) as r:
            json.loads(r.read().decode("utf-8", "replace"))
        log("alert sent")
        return True
    except Exception as e:
        log("send err %r" % e)
        return False


def mtime_age(p):
    try:
        return time.time() - os.path.getmtime(p)
    except Exception:
        return None


def load_state():
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except Exception:
        return {}


def save_state(st):
    try:
        json.dump(st, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception:
        pass


def latest_bar_age(tag, bar):
    """数据文件最后一根 K 线的年龄（秒）"""
    p = os.path.join(DATA, "%s_%s.csv" % (tag, bar))
    try:
        with open(p, encoding="utf-8") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 400))
            last = f.read().strip().split("\n")[-1]
        ts = last.split(",")[0].strip().strip('"')
        dt = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
        return (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds()
    except Exception as e:
        return None


def main():
    issues = []
    # ① 数据新鲜：1d 应 < 30h，4h 应 < 6h，1h 应 < 3h
    for tag in ("btc", "eth", "xrp", "sol"):
        for bar, limit in (("1d", 30 * 3600), ("4h", 6 * 3600), ("1h", 3 * 3600)):
            age = latest_bar_age(tag, bar)
            if age is None:
                issues.append("数据缺失 %s_%s" % (tag, bar))
            elif age > limit:
                issues.append("数据陈旧 %s_%s：最后一根 %.1f 小时前" % (tag, bar, age / 3600))
    # ② 引擎产出：< 5h
    a = mtime_age(os.path.join(TDIR, "engine_signal.json"))
    if a is None:
        issues.append("引擎信号文件缺失")
    elif a > 5 * 3600:
        issues.append("引擎信号陈旧：%.1f 小时未更新" % (a / 3600))
    # ③ 影子账本：< 26h
    a = mtime_age(os.path.join(TDIR, "shadow", "trend_shadow.jsonl"))
    if a is None:
        issues.append("影子账本缺失")
    elif a > 26 * 3600:
        issues.append("影子账本陈旧：%.1f 小时未更新" % (a / 3600))
    # ④ 纪律锁：文件存在即可（有违规会自己告警）
    if not os.path.exists(os.path.join(TDIR, "discipline_state.json")):
        issues.append("纪律锁状态文件缺失（守卫可能没跑）")

    st = load_state()
    now = time.time()
    if not issues:
        st["last_ok"] = now
        save_state(st)
        log("health OK")
        return

    # 去重告警（同类问题 6h 内只报一次）
    key = "|".join(sorted(i.split("：")[0] for i in issues))
    if now - float(st.get("last_report", 0)) < REPORT_COOLDOWN and st.get("last_key") == key:
        log("issues (suppressed): %s" % "; ".join(issues))
        return
    st["last_report"] = now
    st["last_key"] = key
    save_state(st)

    msg = "🩺【VPS 健康巡检异常】\n" + "\n".join("• " + i for i in issues) + "\n\n请检查 VPS（数据/引擎/影子账本）。"
    ding_send(msg)
    log("issues reported: %s" % "; ".join(issues))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        log("FATAL " + traceback.format_exc()[-400:])
