# -*- coding: utf-8 -*-
"""crypto_vps_update.py - VPS 加密数据日更：拉最近窗口 → 与全历史合并去重（幂等）。
cron 每日 08:10 UTC（= 本地 16:10）跑，赶在 08:20 影子账本前。
"""
import os, sys, time
sys.path.insert(0, "/root/quant_system")
import pandas as pd
from fetch_crypto_update import fetch_recent

DATA = "/root/quant_system/data/crypto"
SYMS = ["btc", "eth", "xrp", "sol"]
BARS = [("1D", "1d", 30), ("4H", "4h", 200), ("1H", "1h", 500)]


def main():
    print("crypto_vps_update", flush=True)
    for tag in SYMS:
        for okx_bar, name, n in BARS:
            rows = fetch_recent(tag.upper() + "-USDT-SWAP", okx_bar, n)
            recs = []
            for ts in sorted(rows):
                c = rows[ts]
                recs.append({"time": pd.to_datetime(ts, unit="ms", utc=True).strftime("%Y-%m-%dT%H:%M:%SZ"),
                             "open": float(c[1]), "high": float(c[2]), "low": float(c[3]),
                             "close": float(c[4]), "volume": float(c[5])})
            new = pd.DataFrame(recs)
            p = os.path.join(DATA, tag + "_" + name + ".csv")
            if os.path.exists(p) and len(new):
                old = pd.read_csv(p)
                tc = old.columns[0]
                merged = pd.concat([old, new]).drop_duplicates(subset=[tc], keep="last").sort_values(tc).reset_index(drop=True)
            elif len(new):
                merged = new
            else:
                print("%s_%s: 拉取为空，跳过" % (tag, name), flush=True)
                continue
            merged.to_csv(p, index=False)
            print("%s_%s: %d 根, 最新 %s" % (tag, name, len(merged), merged[merged.columns[0]].iloc[-1]), flush=True)
            time.sleep(0.2)
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
