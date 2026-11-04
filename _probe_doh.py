# -*- coding: utf-8 -*-
"""复现 DoH 兜底：强制系统 DNS 路径抛 DNSError，观察是否由 DoH 救回。

用法: python _probe_doh.py
判据: 强制失败后仍能拿到 SeriesFilter 结果 = 兜底通路有效。
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# 先加载被测模块（避免 tkinter 初始化：模块内 GUI 在 __main__ 或类里，import 安全）
spec = importlib.util.spec_from_file_location(
    "abc_app", os.path.join(HERE, "asus_bios_checker.py"))
mod = importlib.util.module_from_spec(spec)
sys.modules["abc_app"] = mod
spec.loader.exec_module(mod)

from curl_cffi import requests as cr
try:
    from curl_cffi.requests.errors import DNSError
except Exception:                                  # 兼容不同版本导出位置
    from curl_cffi.requests.exceptions import DNSError

_real_get = cr.Session.get
calls = []


def fake_get(self, url, **kw):
    doh = kw.get("doh_url")
    calls.append(doh)
    if not doh:
        raise DNSError("Failed to perform, curl: (6) Could not resolve host: "
                       "odinapi.asus.com. See https://curl.se/libcurl/c/libcurl-errors.html")
    return _real_get(self, url, **kw)


cr.Session.get = fake_get
try:
    api = mod.AsusApi()
    j = api._get_json(mod.API_SERIES_FILTER)
    ok = isinstance(j, dict) and bool(j)
    print("PASS 兜底救回：拿到 %d 个顶层键" % len(j or {}))
    print("     尝试序列: %r" % (calls,))
except Exception as e:
    print("FAIL 兜底未救回: %s %s" % (type(e).__name__, str(e)[:200]))
    print("     尝试序列: %r" % (calls,))
finally:
    cr.Session.get = _real_get
