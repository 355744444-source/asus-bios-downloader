# -*- coding: utf-8 -*-
"""兜底链完整性探针：DoH 逐个都要试到，不能被中途的非解析错误截断。

场景（两种入口：目录 JSON / 下载直链）：
  S1 系统DNS 失败 → 阿里DoH 抛非解析错误（如 SSL）→ 腾讯DoH 正常
     期望：最终成功（备用解析链走完）
  S2 系统DNS 失败 → 阿里DoH 解析失败 → 腾讯DoH 正常
     期望：最终成功

用法: python _probe_doh_chain.py
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "abc_app", os.path.join(HERE, "asus_bios_checker.py"))
mod = importlib.util.module_from_spec(spec)
sys.modules["abc_app"] = mod
spec.loader.exec_module(mod)

from curl_cffi import requests as cr
try:
    from curl_cffi.requests.errors import DNSError, SSLError
except Exception:
    from curl_cffi.requests.exceptions import DNSError, SSLError

ALI, TENCENT = mod.DOH_URLS[0], mod.DOH_URLS[1]
DNS_MSG = ("Failed to perform, curl: (6) Could not resolve host: "
           "odinapi.asus.com. See https://curl.se/libcurl/c/libcurl-errors.html")


class Runner:
    """按 doh_url 分派的假网络：记录实际尝试过的解析通道。"""

    def __init__(self, ali_mode):
        self.ali_mode = ali_mode          # 'ssl' | 'dns'
        self.seen = []

    def dispatch(self, doh, real):
        self.seen.append(doh or "system")
        if doh is None:
            raise DNSError(DNS_MSG)
        if doh == ALI:
            if self.ali_mode == "ssl":
                raise SSLError("Failed to perform, curl: (35) OpenSSL SSL_connect")
            raise DNSError(DNS_MSG)
        return real()                     # 腾讯 DoH：放行到真实网络


def run_json(mode):
    r = Runner(mode)
    real = cr.Session.get

    def fake(self, url, **kw):
        return r.dispatch(kw.get("doh_url"), lambda: real(self, url, **kw))

    cr.Session.get = fake
    try:
        j = mod.AsusApi()._get_json(mod.API_SERIES_FILTER)
        return True, r.seen, len(j or {})
    except Exception as e:
        return False, r.seen, "%s: %s" % (type(e).__name__, str(e)[:80])
    finally:
        cr.Session.get = real


def run_download(mode):
    r = Runner(mode)
    real = cr.get

    def fake(url, **kw):
        return r.dispatch(kw.get("doh_url"), lambda: real(url, **kw))

    cr.get = fake
    try:
        resp = mod._cr_get_dns_safe(mod.DL_CDN + "/", timeout=(5, 10))
        return True, r.seen, getattr(resp, "status_code", "?")
    except Exception as e:
        return False, r.seen, "%s: %s" % (type(e).__name__, str(e)[:80])
    finally:
        cr.get = real


fails = 0
for tag, fn in (("目录JSON", run_json), ("下载直链", run_download)):
    for mode, label in (("ssl", "S1 阿里DoH报SSL错误"), ("dns", "S2 阿里DoH解析失败")):
        ok, seen, info = fn(mode)
        # 判据：腾讯 DoH 必须被尝试到
        tried_tencent = TENCENT in seen
        good = ok and tried_tencent
        if not good:
            fails += 1
        print("%s %-12s %-18s 尝试序列=%s 结果=%s"
              % ("PASS" if good else "FAIL", tag, label, seen, info))
print("\n合计 FAIL = %d" % fails)
