# -*- coding: utf-8 -*-
"""okx_candles.py - OKX K 线拉取库（只读，不写文件，供 VPS 日更脚本调用）。
2026-10-01 立：原先 crypto_vps_update.py 从 fetch_crypto_update.py 导入，
该脚本被改名（防覆盖）后导入失败、数据更新停了 4 天。现抽出独立库，职责单一。
"""
import urllib.request, json, time

BASE = "https://www.okx.com/api/v5/market/history-candles"


def fetch_page(symbol, bar, after_ms, limit=100):
    url = "%s?instId=%s&bar=%s&after=%d&limit=%d" % (BASE, symbol, bar, after_ms, limit)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for _ in range(3):
        try:
            r = urllib.request.urlopen(req, timeout=20)
            d = json.loads(r.read())
            if d.get("code") == "0":
                return d.get("data", [])
        except Exception:
            time.sleep(1)
    return None


def fetch_recent(symbol, bar, n_bars):
    """拉最近 n_bars 根，返回 {ts_ms: candle} 字典（新→旧翻页）。"""
    rows = {}
    cur = int(time.time() * 1000)
    pages = (n_bars // 100) + 2
    for _ in range(pages):
        data = fetch_page(symbol, bar, cur, 100)
        if not data:
            break
        oldest = None
        for c in data:
            ts = int(c[0])
            if ts not in rows:
                rows[ts] = c
            if oldest is None or ts < oldest:
                oldest = ts
        if oldest is None:
            break
        cur = oldest
        time.sleep(0.12)
    return rows
