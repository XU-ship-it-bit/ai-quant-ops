# -*- coding: utf-8 -*-
"""analyze_usage.py - 从 DSH 会话缓存里统计真实 token 用量（按天）。"""
import io, json, datetime, collections

P = r"C:\Users\user\.dsh\storages\session_projcache.json"

raw = io.open(P, encoding="utf-8", errors="replace").read()
try:
    data = json.loads(raw)
except Exception as e:
    print("解析失败:", e)
    raise SystemExit(1)

inp = collections.Counter()   # 日期 -> inputTokens
outp = collections.Counter()  # 日期 -> outputTokens
cnt = collections.Counter()


def walk(node):
    """递归找带 token 字段的字典"""
    if isinstance(node, dict):
        it = node.get("inputTokens") or node.get("promptTokens") or 0
        ot = node.get("outputTokens") or node.get("completionTokens") or 0
        ts = node.get("time") or node.get("ts") or node.get("createdAt") or node.get("updatedAt")
        if ot or it:
            day = "unknown"
            try:
                v = float(ts)
                if v > 1e12:
                    v = v / 1000.0
                day = datetime.datetime.fromtimestamp(v).strftime("%Y-%m-%d")
            except Exception:
                pass
            inp[day] += int(it or 0)
            outp[day] += int(ot or 0)
            cnt[day] += 1
        for v in node.values():
            walk(v)
    elif isinstance(node, list):
        for v in node:
            walk(v)


walk(data)

print("=== 按日 token 用量（DSH 会话缓存）===")
ti = to = 0
for day in sorted(set(list(inp.keys()) + list(outp.keys()))):
    i, o = inp[day], outp[day]
    ti += i
    to += o
    print("%-12s 记录 %5d 条 | 输入 %10s | 输出 %9s" % (day, cnt[day], format(i, ","), format(o, ",")))
print("-" * 62)
print("合计: 输入 %s | 输出 %s | 总 %s tokens" % (format(ti, ","), format(to, ","), format(ti + to, ",")))
