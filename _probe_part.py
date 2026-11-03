# -*- coding: utf-8 -*-
"""复现 do_download 取消路径的 .part 残留缺陷（红→绿验证用）。

用法:
    python _probe_part.py            # 当前实现
断言:
    取消（progress_cb 返回 False）后，<dest>.part 不应残留。
"""
import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def load_mod():
    spec = importlib.util.spec_from_file_location(
        "asus_mod", os.path.join(HERE, "asus_bios_checker.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["asus_mod"] = m
    spec.loader.exec_module(m)
    return m


class FakeResp:
    status_code = 200
    headers = {"Content-Length": "1000000"}

    def iter_content(self, n):
        for _ in range(10):
            yield b"x" * 100000

    def close(self):
        pass


def main():
    m = load_mod()
    m._cr_get_dns_safe = lambda *a, **k: FakeResp()

    d = tempfile.mkdtemp(prefix="probe_part_")
    dest = os.path.join(d, "bios.zip")
    part = dest + ".part"

    calls = {"n": 0}

    def cb(done, total):
        calls["n"] += 1
        return False              # 用户点取消

    ok, path, err, sha = m.do_download(
        "https://example.invalid/x.zip", dest, progress_cb=cb,
        cancel_event=None, expect_size=0)

    print("progress_cb 调用次数 :", calls["n"])
    print("返回                : ok=%r path=%r err=%r sha=%r" % (ok, path, err, sha))
    print("dest 存在            :", os.path.isfile(dest))
    print(".part 残留           :", os.path.isfile(part),
          "(%d 字节)" % (os.path.getsize(part) if os.path.isfile(part) else 0))
    print("目录内容            :", sorted(os.listdir(d)))

    bad = os.path.isfile(part)
    print("\n判定:", "FAIL —— 取消后残留 .part" if bad else "PASS —— 取消后无残留")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
