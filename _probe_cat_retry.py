# -*- coding: utf-8 -*-
"""目录更新失败后的收尾断言（v2.3.1）。

B1 有缓存：文案点出缓存款数 + 给出重试倒计时，且排了自动重试
B2 无缓存：不得再说「仍可使用本地缓存的型号」，要说明需要联网
B3 重复失败不叠加排队
B4 撤销后无待执行任务
B5 成功一次后重试计数归零
B6 重试次数用尽后给出手动刷新指引

用法: python _probe_cat_retry.py
"""
import importlib.util
import os
import sys
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("abc_app", os.path.join(HERE, "asus_bios_checker.py"))
m = importlib.util.module_from_spec(spec)
sys.modules["abc_app"] = m
spec.loader.exec_module(m)

PASS = FAIL = 0


def ck(name, cond, note=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS %s  %s" % (name, note))
    else:
        FAIL += 1
        print("FAIL %s  %s" % (name, note))


root = tk.Tk()
app = m.App(root)
root.deiconify()
root.geometry("1100x760")
root.update_idletasks()

# 直接截获写入状态栏的文本，绕开消息泵时序（本探针只验文案与调度）
seen = []
app._set_status = lambda msg, kind="info": seen.append((msg, kind))

ERR = "域名解析失败，已依次尝试 2 个备用解析通道（DoH）仍未成功"

# ---- B1 有缓存
app.catalog = {"total": 910, "brands": []}
app._apply_catalog(None, ERR)
msg = seen[-1][0] if seen else ""
ck("B1.1 文案点出缓存款数", "910" in msg and "可继续使用" in msg, repr(msg[:52]))
ck("B1.2 排了自动重试", app._cat_retry_id is not None and app._cat_retry_n == 1,
   "id=%s n=%d" % (app._cat_retry_id, app._cat_retry_n))
ck("B1.3 文案给出重试倒计时", "秒后自动重试（第 1/" in msg, repr(msg[-24:]))
ck("B1.4 状态级别为错误", seen[-1][1] == "err", seen[-1][1])

# ---- B3 重复失败不叠加
first_id = app._cat_retry_id
app._apply_catalog(None, ERR)
ck("B3 重复失败不叠加排队", app._cat_retry_id == first_id and app._cat_retry_n == 1,
   "n=%d" % app._cat_retry_n)

# ---- B4 撤销
app._cancel_cat_retry()
ck("B4 撤销后无待执行任务", app._cat_retry_id is None)

# ---- B5 成功后归零
app._apply_catalog({"total": 5, "updated": "2026-11-04", "brands": []}, None)
ck("B5 成功后重试计数归零", app._cat_retry_n == 0 and app._cat_retry_id is None,
   "n=%d" % app._cat_retry_n)

# ---- B2 无缓存
app.catalog = None
app._apply_catalog(None, ERR)
msg = seen[-1][0] if seen else ""
ck("B2 无缓存时不说「仍可使用本地缓存」",
   "仍可使用本地缓存" not in msg and "可继续使用" not in msg, repr(msg[:52]))
ck("B2b 无缓存时说明需要联网", "需要联网" in msg, repr(msg[:52]))

# ---- B6 重试用尽后给手动指引
app._cat_retry_n = len(m.CAT_RETRY_DELAYS)
app._cat_retry_id = None
app._apply_catalog(None, ERR)
msg = seen[-1][0] if seen else ""
last = msg.split("\n")[-1]
ck("B6 重试用尽后提示手动刷新",
   "↻" in last and "秒后自动重试" not in last and "已自动重试" in last, repr(last))

root.destroy()
print("\nPASS %d / FAIL %d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
