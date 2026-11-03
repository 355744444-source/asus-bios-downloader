# -*- coding: utf-8 -*-
"""五层审计断言（源码态，离屏）。

纪律：**先写断言，再改代码**。每条断言都必须在「修复前」红、在「修复后」绿。
段内任何未捕获异常记为该段失败，不中断整份报告（否则缺接口时连基线都拿不到）。

  A1  线程与并发：目录刷新重入保护
  A2  调度与健壮性：selftest 断网不崩且必落日志
  A3  磁盘与文件系统：只读目录（ACL 拒绝）下数据文件回退
  A4  配置持久化：save_dir 存得下、读得回、应用设置不丢
  A5  配置持久化：save_cfg 失败要如实上报
  A6  UI 布局：右栏型号标题不溢出
  A7  磁盘：写盘临时名唯一 + 并发写不互踩
  A8  磁盘：源码态 _app_dir 不跟随 cwd
  A9  磁盘与策略：下载大小交叉校验（含参数打通）
  A10 回归：v2.0.1 左栏宽度收口不被打破
  A11 调度：空闲停表 + put 自动唤醒
  A12 内存：像素宽度缓存有上界
  A13 并发：后台线程入队不阻塞（Tk 只能主线程碰）
  A14 UI：更新说明列按实测像素截断
  A15 折叠：目录树展开状态跨重建保留
  A16 折叠：展开全部 / 折叠全部入口
  A17 折叠：重建后滚动位置回位
  A18 UI：BIOS 列表「更新说明」悬停看全文
  A19 UI：主板提示框 wraplength 跟随窗口
  A20 UI：悬停提示浮窗屏幕边界收拢
  A21 UI：状态栏超长文本不挤右侧信息
  A22 UI：目录树缩进收紧（各层文字更靠左边框；长型号名不被硬裁）
  A23 UI：左栏目录树「不截断」（比例上限取代固定 300px）
  A24 UI：BIOS 表列宽总和 ≤ 控件宽（说明列不被硬裁）
  A25 UI：v2.1.0 视觉焕新（三级底面 / 卡片描边 / 样式作用域不越界）

运行：<venv>/Scripts/python.exe _v_audit.py [结果文件]
"""
import ctypes
import importlib.util
import io
import json
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import http.server

HERE = os.path.dirname(os.path.abspath(__file__))
RESULT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "_v_audit_result.txt")
sys.argv = ["audit"]

spec = importlib.util.spec_from_file_location("abc", os.path.join(HERE, "asus_bios_checker.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

import tkinter as tk          # noqa: E402
from tkinter import ttk       # noqa: E402

PASS, FAIL = [], []
LINES = []


def p(s):
    LINES.append(str(s))


def ck(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    p("%s %s %s" % ("[OK]" if cond else "[NG]", name, extra))


def section(fn):
    """跑一个断言段落；段内异常记一条失败，不让整份报告被打断。"""
    p("")
    p("== %s ==" % (fn.__doc__ or fn.__name__))
    try:
        fn()
    except Exception as e:
        ck("§%s 段落跑完" % fn.__name__, False, repr(e)[:160])
        p("    " + traceback.format_exc().replace("\n", "\n    ")[:900])


def _cat_json():
    with io.open(os.path.join(HERE, "asus_mb_catalog.json"), encoding="utf-8") as f:
        return json.load(f)


def _pump(root, n=6, dt=0.03):
    for _ in range(n):
        root.update()
        time.sleep(dt)


def _grab(hwnd):
    """PrintWindow 抓真实窗口像素（视觉类断言用）。

    为什么必须抓像素而不是读 style：ttk 的 `style.lookup()` 返回的是**配置值**，
    与**实际画出来的颜色**可以不一致 —— v2.1.0 就踩过两次：
      · Card.TFrame 的 bordercolor 配的是 C_BORDER，但 relief="flat" 让它按背景色
        描边，"配上但没画"，lookup 查不出来；
      · 空文本的 ttk.Label 渲染成画布色而非其 style 声明的卡片色。
    只读配置的断言在这两处都会给出假绿，所以视觉断言一律走真实像素。
    """
    from ctypes import wintypes
    from PIL import Image

    class _H(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    class _BI(ctypes.Structure):
        _fields_ = [("bmiHeader", _H), ("bmiColors", wintypes.DWORD * 3)]

    u, g = ctypes.windll.user32, ctypes.windll.gdi32
    rc = wintypes.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(rc))
    w, h = rc.right - rc.left, rc.bottom - rc.top
    hdc = u.GetWindowDC(hwnd)
    mdc = g.CreateCompatibleDC(hdc)
    bmp = g.CreateCompatibleBitmap(hdc, w, h)
    g.SelectObject(mdc, bmp)
    u.PrintWindow(hwnd, mdc, 2)
    bi = _BI()
    bi.bmiHeader.biSize = ctypes.sizeof(_H)
    bi.bmiHeader.biWidth = w
    bi.bmiHeader.biHeight = -h
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    buf = ctypes.create_string_buffer(w * h * 4)
    g.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
    g.DeleteObject(bmp)
    g.DeleteDC(mdc)
    u.ReleaseDC(hwnd, hdc)
    return img, (rc.left, rc.top)


def _rgb(hexs):
    t = hexs.lstrip("#")
    return (int(t[0:2], 16), int(t[2:4], 16), int(t[4:6], 16))


def _near(c, hexs, tol=6):
    t = _rgb(hexs)
    return all(abs(c[i] - t[i]) <= tol for i in range(3))


def _lum(c):
    r, g, b = [v / 255.0 for v in c[:3]]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _px(img, ox, oy):
    """返回 (widget, dx, dy) -> 像素 的取值器。"""
    px = img.load()

    def at(w, dx, dy):
        return px[w.winfo_rootx() - ox + dx, w.winfo_rooty() - oy + dy]
    return at


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


m.App._boot = lambda self: None      # 本脚本自己控制初始化，别让 after(300) 插手

# 本脚本对产品数据文件只读；万一被谁改写了，最后无条件还原（并留痕供排查）
_CACHE = m._cache_path()
_CACHE_BAK = io.open(_CACHE, "rb").read() if os.path.isfile(_CACHE) else None
_CFG0 = m._cfg_path()
_CFG0_BAK = io.open(_CFG0, "rb").read() if os.path.isfile(_CFG0) else None
_WRITES = []
_SAVE_REAL = m.save_catalog


def _save_traced(cat):
    _WRITES.append((len(str(cat)), "".join(traceback.format_stack()[-3:-1])[-160:]))
    return _SAVE_REAL(cat)


m.save_catalog = _save_traced


# ================================================================ A1 重入保护
def a1():
    """A1 线程与并发：目录刷新重入保护"""
    root = tk.Tk(); root.withdraw()
    try:
        app = m.App(root)
        app.catalog = _cat_json()
        live = {"now": 0, "max": 0, "total": 0}
        lk = threading.Lock()
        ev = threading.Event()
        _of, _os = m.fetch_catalog, m.save_catalog

        def _slow(api, progress=None):
            with lk:
                live["now"] += 1
                live["total"] += 1
                live["max"] = max(live["max"], live["now"])
            ev.set()
            time.sleep(1.0)
            with lk:
                live["now"] -= 1
            return {"updated": "probe", "total": 0, "brands": []}

        m.fetch_catalog = _slow
        m.save_catalog = lambda c: True
        try:
            for _ in range(3):
                app._refresh_now(None)
            ev.wait(3)
            time.sleep(0.35)
            ck("A1.1 连点 3 次只起 1 个拉取", live["total"] == 1,
               "累计=%d 并发峰值=%d" % (live["total"], live["max"]))
            st = str(app.btn_cat_refresh.cget("state"))
            ck("A1.2 刷新在途时 ↻ 按钮禁用", st == "disabled", "state=%s" % st)
            time.sleep(1.2)
            p("    诊断 泵前: q=%d polling=%r cat_running=%r"
              % (app.q.qsize(), getattr(app, "_polling", None), app._cat_running))
            _pump(root, 8)
            p("    诊断 泵后: q=%d polling=%r cat_running=%r catalog=%r"
              % (app.q.qsize(), getattr(app, "_polling", None), app._cat_running,
                 bool(app.catalog)))
            st2 = str(app.btn_cat_refresh.cget("state"))
            ck("A1.3 刷新结束后 ↻ 按钮恢复", st2 != "disabled", "state=%s" % st2)
        finally:
            m.fetch_catalog, m.save_catalog = _of, _os
            app._cat_running = False
    finally:
        root.destroy()


# ================================================================ A2 selftest 断网
def a2():
    """A2 调度与健壮性：selftest 断网不崩且必落日志"""
    log = os.path.join(m._app_dir(), "asus_bios_checker_selftest.log")
    bak = io.open(log, "rb").read() if os.path.isfile(log) else None
    _of, _og = m.fetch_catalog, m.AsusApi.get_chipset_codes

    def _boom(*a, **k):
        raise RuntimeError("模拟无网")

    try:
        try:
            os.remove(log)
        except Exception:
            pass
        m.fetch_catalog = _boom
        m.AsusApi.get_chipset_codes = _boom
        try:
            rc = m.selftest()
            ck("A2.1 断网时 selftest 不抛异常", True, "rc=%r" % (rc,))
        except Exception as e:
            ck("A2.1 断网时 selftest 不抛异常", False, repr(e)[:120])
        has_log = os.path.isfile(log)
        ck("A2.2 断网时仍写出自检日志", has_log)
        txt = io.open(log, encoding="utf-8", errors="replace").read() if has_log else ""
        ck("A2.3 日志里有失败说明", ("异常" in txt or "失败" in txt),
           "len=%d 尾部=%r" % (len(txt), txt[-60:]))
    finally:
        m.fetch_catalog, m.AsusApi.get_chipset_codes = _of, _og
        try:
            if os.path.isfile(log):
                os.remove(log)
            if bak is not None:
                io.open(log, "wb").write(bak)
        except Exception:
            pass


# ================================================================ A3 只读目录
def a3():
    """A3 磁盘与文件系统：只读目录（ACL 拒绝）下数据文件回退"""
    rod = tempfile.mkdtemp(prefix="_v_ro_")
    user = os.environ.get("USERNAME") or ""
    _oa = m._app_dir
    try:
        subprocess.run(["icacls", rod, "/deny", "%s:(W)" % user],
                       capture_output=True, text=True)
        probed = os.access(rod, os.W_OK)
        m._app_dir = lambda: rod
        try:
            m._writable_cache.clear()
        except Exception:
            pass
        cp = m._cfg_path()
        ck("A3.1 只读目录下 _cfg_path 落临时目录",
           os.path.normcase(os.path.dirname(cp)) == os.path.normcase(tempfile.gettempdir()),
           "os.access(W_OK)=%r 实际路径=%s" % (probed, cp))
        ck("A3.2 只读目录下 save_cfg 成功",
           m.save_cfg({"refresh_days": 5, "show_platforms": None,
                       "chip_filter": None}) is True)
        ck("A3.3 只读目录下 load_cfg 能读回", m.load_cfg().get("refresh_days") == 5)
    finally:
        m._app_dir = _oa
        try:
            m._writable_cache.clear()
        except Exception:
            pass
        try:
            os.remove(os.path.join(tempfile.gettempdir(), m.CFG_FILE))
        except Exception:
            pass
        subprocess.run(["icacls", rod, "/remove:d", user], capture_output=True, text=True)
        shutil.rmtree(rod, ignore_errors=True)


# ================================================================ A4/A5 配置持久化
def a4():
    """A4/A5 配置持久化：save_dir 与写盘失败上报"""
    ck("A4.1 DEFAULT_CFG 含 save_dir", "save_dir" in m.DEFAULT_CFG,
       "keys=%s" % sorted(m.DEFAULT_CFG))
    cp = m._cfg_path()
    cbak = io.open(cp, "rb").read() if os.path.isfile(cp) else None
    root = None
    try:
        d = tempfile.mkdtemp(prefix="_v_save_")
        m.save_cfg({"refresh_days": 7, "show_platforms": None,
                    "chip_filter": None, "save_dir": d})
        ck("A4.2 save_dir 能存能读", m.load_cfg().get("save_dir") == d, repr(m.load_cfg()))
        root = tk.Tk(); root.withdraw()
        app = m.App(root)
        ck("A4.3 启动时把 save_dir 读进 last_dir", app.last_dir == d,
           "last_dir=%r" % (app.last_dir,))
        _osd = m.save_cfg
        seen = {}
        m.save_cfg = lambda cfg: (seen.clear(), seen.update(cfg), True)[2]
        try:
            # A4.4：换一个**新的**目录，必须当场落盘（已存过的目录无需重复写）
            d2 = tempfile.mkdtemp(prefix="_v_save2_")
            app.last_dir = None
            try:
                app._remember_dir(d2)
                ck("A4.4 选定目录后立刻落盘", seen.get("save_dir") == d2,
                   "写入=%r" % (seen,))
            except AttributeError:
                ck("A4.4 选定目录后立刻落盘", False, "_remember_dir 不存在")
            # A4.5：应用设置时不能把已记住的目录丢掉
            seen.clear()
            app.cfg = {"refresh_days": 7, "show_platforms": None,
                       "chip_filter": None, "save_dir": d}
            app._settings_apply(tk.Toplevel(root), tk.StringVar(value="7 天"),
                                {"Intel": tk.BooleanVar(value=True)}, ["Intel"],
                                tk.IntVar(value=0), None, None)
            ck("A4.5 应用设置不丢 save_dir", seen.get("save_dir") == d,
               "写入=%r" % (seen,))
        finally:
            m.save_cfg = _osd
        # ---- A5：保存失败必须如实说
        m.save_cfg = lambda cfg: False
        try:
            while True:
                try:
                    app.q.get_nowait()
                except Exception:
                    break
            app._settings_apply(tk.Toplevel(root), tk.StringVar(value="7 天"),
                                {"Intel": tk.BooleanVar(value=True)}, ["Intel"],
                                tk.IntVar(value=0), None, None)
            st = []
            while True:
                try:
                    x = app.q.get_nowait()
                except Exception:
                    break
                if x and x[0] == "status":
                    st.append(x[1])
            ck("A5.1 保存失败时如实报错",
               any(("失败" in t or "无法" in t or "写入" in t) for t in st), repr(st))
        finally:
            m.save_cfg = _osd
    finally:
        if root is not None:
            root.destroy()
        if cbak is not None:
            io.open(cp, "wb").write(cbak)
        else:
            try:
                os.remove(cp)
            except Exception:
                pass


# ================================================================ A6 右栏标题
LONG_NAME = "ROG MAXIMUS XI HERO (WI-FI) Call of Duty - Black Ops 4 Edition"


def a6():
    """A6 UI 布局：右栏型号标题不溢出"""
    for dpi in (96, 144):
        m._apply_dpi_scale(dpi)
        root = tk.Tk()
        try:
            app = m.App(root)
            root.deiconify()
            root.geometry("%dx%d" % m._fit_size(1100, 760, 40))
            _pump(root, 6)
            app.catalog = _cat_json()
            app._populate_tree()
            tag = "DPI%d%%" % (dpi * 100 // 96)
            for nm, title in (("短名", "ROG STRIX B850-A GAMING WIFI"), ("超长名", LONG_NAME)):
                try:
                    app._set_board_title(title)
                except AttributeError:
                    app.v_board_title.set(title)
                _pump(root, 5)
                trow = app.btn_openpage.master
                lbls = [c for c in trow.winfo_children() if isinstance(c, ttk.Label)]
                lbl = lbls[0] if lbls else None
                need = (lbl.winfo_reqwidth() + app.btn_openpage.winfo_reqwidth() + m.S(16)) \
                    if lbl else 10 ** 9
                shown = lbl.cget("text") if lbl else ""
                ck("A6 %s %s 标题行不溢出" % (tag, nm), need <= trow.winfo_width(),
                   "需=%d 行宽=%d 显示=%r" % (need, trow.winfo_width(), str(shown)[:44]))
                # 直击症状：标题过长时右侧按钮会被 pack 挤成 0 宽（用户看到的就是
                # 「按钮不见了」）—— 断言它真实占位宽 ≥ 请求宽
                bw, br = app.btn_openpage.winfo_width(), app.btn_openpage.winfo_reqwidth()
                ck("A6 %s %s 右侧按钮未被挤没" % (tag, nm), bw >= br,
                   "按钮宽=%d 需=%d" % (bw, br))
            ck("A6 DPI%d%% 超长名已截断但仍留完整名（悬停可看）"
               % (dpi * 100 // 96),
               app.v_board_title.get() != LONG_NAME
               and getattr(app, "_title_full", None) == LONG_NAME,
               "显示=%r 完整=%r" % (app.v_board_title.get()[:30],
                                    getattr(app, "_title_full", None)))
        finally:
            root.destroy()


# ================================================================ A7 临时名唯一
def a7():
    """A7 磁盘：写盘临时名唯一 + 并发写不互踩"""
    src = io.open(os.path.join(HERE, "asus_bios_checker.py"), encoding="utf-8").read()
    ck("A7.1 临时名按写者唯一（_tmp_path）", "_tmp_path" in src,
       "存在 _tmp_path=%s" % ("_tmp_path" in src))
    cache = m._cache_path()
    bak = io.open(cache, "rb").read() if os.path.isfile(cache) else None
    try:
        bad = []

        def mk(t):
            return {"updated": t, "total": 1, "brands": [
                {"name": t, "count": 1, "chipsets": [{"name": "X", "boards": [
                    {"id": "%s%d" % (t, i), "title": t * 300, "slug": "%s%d" % (t, i)}
                    for i in range(400)]}]}]}

        for _ in range(6):
            ths = [threading.Thread(target=m.save_catalog, args=(mk(t),))
                   for t in ("A", "B")]
            [x.start() for x in ths]
            [x.join() for x in ths]
            try:
                got = json.loads(io.open(cache, encoding="utf-8").read()).get("updated")
                if got not in ("A", "B"):
                    bad.append("内容异常:%r" % (got,))
            except Exception as e:
                bad.append(repr(e)[:60])
        ck("A7.2 6 轮并发写盘内容始终合法", not bad, repr(bad))
        left = [f for f in os.listdir(HERE) if f.endswith(".tmp")]
        ck("A7.3 无 .tmp 残留", not left, repr(left))
    finally:
        if bak is not None:
            io.open(cache, "wb").write(bak)


# ================================================================ A8 _app_dir
def a8():
    """A8 磁盘：源码态 _app_dir 不跟随 cwd"""
    tmp = tempfile.mkdtemp(prefix="_v_cwd_")
    old = os.getcwd()
    try:
        os.chdir(tmp)
        ck("A8.1 换 cwd 后 _app_dir 仍指向脚本目录",
           os.path.normcase(m._app_dir()) == os.path.normcase(HERE), m._app_dir())
    finally:
        os.chdir(old)
        shutil.rmtree(tmp, ignore_errors=True)
    src = io.open(os.path.join(HERE, "asus_bios_checker.py"), encoding="utf-8").read()
    ck("A8.2 打包态仍用 exe 目录", "sys.executable" in src)


# ================================================================ A9 下载大小校验
A9_FULL = 40000
A9_SHORT = 3000


class _H(http.server.BaseHTTPRequestHandler):
    mode = "full"

    def do_GET(self):
        n = A9_SHORT if _H.mode == "short" else A9_FULL
        self.send_response(200)
        self.send_header("Content-Length", str(n))
        self.end_headers()
        self.wfile.write(b"Z" * n)

    def log_message(self, *a):
        pass


def a9():
    """A9 磁盘与策略：下载大小交叉校验（含参数打通）"""
    import inspect
    has_param = "expect_size" in inspect.signature(m.do_download).parameters
    ck("A9.0 do_download 支持 expect_size", has_param,
       str(inspect.signature(m.do_download)))
    srv = socketserver.TCPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    dst = os.path.join(tempfile.gettempdir(), "_v_size.bin")
    url = "http://127.0.0.1:%d/a.zip" % srv.server_address[1]
    try:
        if has_param:
            _H.mode = "full"
            r = m.do_download(url, dst, expect_size=A9_FULL)
            ck("A9.1 大小相符 -> 成功", r[0] is True, "err=%r" % (r[2],))
            _H.mode = "short"          # HTTP 自洽但比官网标注少 37KB：真截断
            for f in (dst, dst + ".part"):
                try:
                    os.remove(f)
                except Exception:
                    pass
            r2 = m.do_download(url, dst, expect_size=A9_FULL)
            ck("A9.2 大小不符 -> 报错且不落文件",
               r2[0] is False and not os.path.isfile(dst),
               "ok=%r err=%r 残留=%s" % (r2[0], r2[2], os.path.isfile(dst)))
            r3 = m.do_download(url, dst)
            ck("A9.3 未传 expect_size 时不校验（向后兼容）", r3[0] is True)
        else:
            ck("A9.1 大小相符 -> 成功", False, "缺 expect_size 参数")
            ck("A9.2 大小不符 -> 报错且不落文件", False, "缺 expect_size 参数")
            ck("A9.3 未传 expect_size 时不校验（向后兼容）", False, "缺 expect_size 参数")
    finally:
        srv.shutdown()
        srv.server_close()
        for f in (dst, dst + ".part"):
            try:
                os.remove(f)
            except Exception:
                pass
    # A9.4 打通：_on_download 必须把官网标注大小交给下载器
    root = tk.Tk(); root.withdraw()
    try:
        app = m.App(root)
        item = {"download_url": "http://127.0.0.1:1/a.zip", "download_version": "1.0",
                "download_size": 12345, "download_sha256": ""}
        app._iid_item = {"i1": item}
        app.tv.insert("", "end", iid="i1", values=("x",))
        app.tv.selection_set("i1")
        app.last_dir = tempfile.gettempdir()
        seen = {}
        _od = m.do_download

        def _fake(url, dest, progress_cb=None, cancel_event=None, expect_size=0):
            seen["expect_size"] = expect_size
            return True, dest, None, None

        m.do_download = _fake
        try:
            app._on_download()
            for _ in range(60):
                if "expect_size" in seen:
                    break
                time.sleep(0.05)
            ck("A9.4 下载把官网标注字节数交给下载器",
               seen.get("expect_size") == 12345, repr(seen))
        finally:
            m.do_download = _od
    finally:
        root.destroy()


# ================================================================ A10 v2.0.1 回归
def a10():
    """A10 回归：型号列宽度收口策略不被打破（v2.2.0 三栏口径）

    v2.0.1 用「封顶」解决离群长名把整栏拉爆；v2.0.5 用户要求「别截断」，把固定
    300px 改成比例上限。v2.2.0 改为三栏后口径再变一次：**中栏宽度是常量
    PICKER_MODEL_W（窗口无关）**，窗口宽度只决定右栏能吃多少，型号列本身不再
    随窗口伸缩 —— 因此原来那条「列宽不越布局区 45%」的比例上限断言已不存在，
    改判「中栏列宽 == 卡片宽 − 固定开销」且「截断率仍在 3% 以内」。
    """
    m._apply_dpi_scale(96)      # 本段自建 App：别继承 A6 留下的 144，否则 S() 口径错位
    for name in ("PICKER_CHIP_W", "PICKER_MODEL_W", "PICKER_RIGHT_MIN",
                 "PICKER_CARD_PAD", "TREE_SCROLL_W"):
        ck("A10.1 三栏几何常量已定义 %s" % name,
           isinstance(getattr(m, name, None), int) and getattr(m, name) > 0,
           repr(getattr(m, name, None)))
    ck("A10.1b 三栏宽度自洽（三栏 + 两处间距 ≤ 1200 窗口的布局区净宽）",
       not (m.S(m.PICKER_CHIP_W) + m.S(m.PICKER_MODEL_W) + m.S(m.PICKER_RIGHT_MIN)
            + m.S(m.PANE_GAP) * 2 > 1200 - m.S(m.CAT_BODY_PAD)),
       "chip=%d model=%d rightmin=%d 合计=%d"
       % (m.S(m.PICKER_CHIP_W), m.S(m.PICKER_MODEL_W), m.S(m.PICKER_RIGHT_MIN),
          m.S(m.PICKER_CHIP_W) + m.S(m.PICKER_MODEL_W) + m.S(m.PICKER_RIGHT_MIN)
          + m.S(m.PANE_GAP) * 2))
    root = tk.Tk()
    try:
        app = m.App(root)
        root.deiconify()
        _pump(root, 4)
        app.catalog = _cat_json()
        app.cat_updated = app.catalog.get("updated") or ""
        app.cfg["show_platforms"] = None      # 不受本机既有配置影响
        app.cfg["chip_filter"] = None
        app._search_q = ""
        app._populate_tree()
        root.update()
        over = m.S(m.PICKER_CARD_PAD) + m.S(m.TREE_SCROLL_W)
        ck("A10.2 三栏宽度按常量落地",
           app._cf.winfo_width() <= m.S(m.PICKER_CHIP_W) + 1
           and app._mf.winfo_width() <= m.S(m.PICKER_MODEL_W) + 1
           and app._rf.winfo_width() >= m.S(m.PICKER_RIGHT_MIN) - 2,
           "芯片组=%d 型号=%d BIOS=%d"
           % (app._cf.winfo_width(), app._mf.winfo_width(), app._rf.winfo_width()))
        ck("A10.2b 中栏 #0 列宽 == 卡片宽 − 内边距边框 − 滚动条",
           abs(int(app.mod_tree.column("#0", "width"))
               - (app._mf.winfo_width() - over)) <= 1,
           "col0=%d 卡片=%d 开销=%d"
           % (int(app.mod_tree.column("#0", "width")), app._mf.winfo_width(), over))
        # 截断率现在只看**型号列**（原来算的是左栏三级树里的型号名）
        n = len(app._mod_iid_full)
        cut = sum(1 for iid, full in app._mod_iid_full.items()
                  if app.mod_tree.item(iid, "text") != full)
        ck("A10.3 型号列截断比例 <3%", n > 0 and cut < n * 0.03,
           "%d/%d（可用宽 %dpx）" % (cut, n, app._mod_col - m.CAT_TREE_LEAD
                                     - m.CAT_IND_SLOT))
        # v2.0.5：本机窗口下已能做到 0~1 行截断，「cut > 0」不再是正确判据；
        # 但**省略号能力本身**不能被删掉（窗口更窄的机器上仍要用），用合成超长文本验证。
        long_txt = "ROG " + "A" * 200
        shown = app._ellipsize(long_txt, 200)
        ck("A10.3b 省略号兜底仍可用（合成超长文本被裁到预算内 + … 收尾）",
           shown.endswith("…") and app._text_px(shown) <= 200,
           "%r 宽=%d" % (shown[-14:], app._text_px(shown)))
        # 取 3 次最小值：本段之前有 A2/A3/A9 的网络与下载测试，其后台线程收尾
        # 可能压住 CPU，单次采样会测出 148ms 这种与代码无关的假红（实测同一份
        # 代码单独跑稳定 11~14ms）。性能断言必须排除环境干扰，否则会变成误报源。
        times = []
        for _ in range(3):
            t0 = time.perf_counter()
            app._populate_tree()
            root.update_idletasks()
            times.append((time.perf_counter() - t0) * 1000)
        ck("A10.4 目录重建仍快（<50ms，热缓存最小值）", min(times) < 50,
           "最快 %.1fms / 三次=%s" % (min(times), ["%.1f" % t for t in times]))
    finally:
        root.destroy()


# ================================================================ A11 调度
def a11():
    """A11 调度：空闲停表 + 后台任务期间保持心跳"""
    root = tk.Tk(); root.withdraw()
    try:
        app = m.App(root)
        _pump(root, 25, 0.03)
        ck("A11.1 空闲时轮询已停表（不再永续心跳）",
           getattr(app, "_polling", None) is False, "_polling=%r" % (getattr(app, "_polling", None),))
        ck("A11.2 空闲时没有登记的后台任务",
           getattr(app, "_workers", None) == 0, "_workers=%r" % (getattr(app, "_workers", None),))

        def _job():
            app.q.put(("status", "任务进行中", "run"))
            time.sleep(0.25)
            app.q.put(("status", "任务已结束", "ok"))
        try:
            app._spawn(_job)
            _pump(root, 20, 0.03)
            ck("A11.3 后台任务期间消息持续投递",
               app.v_status.get() == "任务已结束", "v_status=%r" % (app.v_status.get(),))
        except AttributeError:
            ck("A11.3 后台任务期间消息持续投递", False, "_spawn 不存在")
        _pump(root, 30, 0.03)
        ck("A11.4 任务结束后重新停表",
           getattr(app, "_polling", None) is False, "_polling=%r" % (getattr(app, "_polling", None),))
    finally:
        root.destroy()


# ================================================================ A12 内存
def a12():
    """A12 内存：像素宽度缓存有上界"""
    cap = getattr(m, "PX_CACHE_MAX", None)
    ck("A12.1 PX_CACHE_MAX 已定义", isinstance(cap, int) and cap > 0, repr(cap))
    root = tk.Tk(); root.withdraw()
    try:
        app = m.App(root)
        if isinstance(cap, int) and cap > 0:
            app._text_px("预热")        # 先同步 _px_dpi，避免预热本身被 DPI 失效清空
            app._px_cache = {"k%d" % i: 1 for i in range(cap + 50)}
            app._text_px("触发一次清理")
            ck("A12.2 缓存超上界后自动收缩", len(app._px_cache) <= cap,
               "len=%d cap=%d" % (len(app._px_cache), cap))
        else:
            ck("A12.2 缓存超上界后自动收缩", False, "缺少 PX_CACHE_MAX，缓存无上界")
    finally:
        root.destroy()


def a13():
    """A13 线程与并发：后台线程入队不得阻塞（Tk 只能主线程碰）"""
    root = tk.Tk(); root.withdraw()
    try:
        app = m.App(root)
        done = threading.Event()

        def _job():
            for i in range(5):
                app.q.put(("status", "bg%d" % i, "run"))
            done.set()

        t0 = time.perf_counter()
        th = threading.Thread(target=_job, daemon=True)
        th.start()
        ok = done.wait(2.0)
        dt = (time.perf_counter() - t0) * 1000
        ck("A13.1 后台线程连续 5 次入队 2s 内返回", ok, "%.0fms" % dt)
        ck("A13.2 后台线程正常退出", not th.is_alive())
    finally:
        root.destroy()


LONG_DESC = ("1. Improve system stability. 2. Update AGESA to 1.2.0.3b. "
             "3. Fix an issue where the system may hang during POST.")


def a14():
    """A14 UI 布局：更新说明列按实测像素截断（不是字符数代理）"""
    for dpi in (96, 144):
        m._apply_dpi_scale(dpi)
        root = tk.Tk()
        try:
            app = m.App(root)
            root.deiconify()
            root.geometry("%dx%d" % m._fit_size(1200, 780, 40))
            _pump(root, 6)
            app.groups = {"BIOS": [{"download_version": "2201",
                                    "download_release": "2026/09/16",
                                    "download_size": 10349400,
                                    "download_description": LONG_DESC}] * 10}
            app.badges = ["BIOS"]
            app._load_badge("BIOS")
            _pump(root, 6)
            tag = "DPI%d%%" % (dpi * 100 // 96)
            iid = app.tv.get_children()[0]
            shown = app.tv.item(iid, "values")[3]
            col = int(app.tv.column("note", "width"))
            avail = app._desc_px()
            w = app._px(shown, app._fm_tree)
            ck("A14 %s 说明不超列宽" % tag, w <= col - m.S(6),
               "显示宽=%d 列宽=%d" % (w, col))
            ck("A14 %s 截断带省略号" % tag, shown.endswith("…"), repr(shown[-14:]))
            ck("A14 %s 列宽利用率 ≥90%%（旧版 desc[:56] 只用到 ~%.0f%%）"
               % (tag, 100.0 * app._px(LONG_DESC[:56], app._fm_tree) / avail),
               w >= avail * 0.90, "显示宽=%d / 可用=%d" % (w, avail))
            # 列变宽后重算（stretch 列在窗口拉伸时变宽，这里直接改列宽验证同一代码路径）
            app.tv.column("note", width=col + m.S(160))
            app._do_desc_retext()
            shown2 = app.tv.item(iid, "values")[3]
            ck("A14 %s 列变宽后说明随之变长" % tag, len(shown2) > len(shown),
               "%d -> %d 字符" % (len(shown), len(shown2)))
            ck("A14 %s 已挂 <Configure> 重算钩子" % tag, bool(app.tv.bind("<Configure>")),
               repr(app.tv.bind("<Configure>")))
        finally:
            root.destroy()


# ================================================================ A15~A21 UI 文字展现 / 折叠
class _Ev(object):
    """伪事件（探针模拟鼠标）。"""

    def __init__(self, x=0, y=0, x_root=0, y_root=0):
        self.x, self.y, self.x_root, self.y_root = x, y, x_root, y_root


def _mk_app(w=1100, h=760, dpi=96):
    """建一个装了真实目录的 App（UI 类断言共用）。"""
    m._apply_dpi_scale(dpi)
    root = tk.Tk()
    app = m.App(root)
    root.deiconify()
    root.geometry("%dx%d" % m._fit_size(w, h, 40))
    _pump(root, 6)
    app.catalog = _cat_json()
    app._populate_tree()
    _pump(root, 4)
    return root, app


def _find(app, name):
    """按完整节点名找回重建后的 iid（iid 每次重建都变）。"""
    for iid, meta in (app._iid_full or {}).items():
        if meta[0] == name:
            return iid
    return None


def _first_chipset(app):
    for pl in app.cat_tree.get_children():
        cs = app.cat_tree.get_children(pl)
        if cs:
            return pl, cs[0]
    return None, None


def _n_open_cs(app):
    """当前展开的平台行数（v2.2.0：芯片组已是叶子，展开态只剩平台这一级）。"""
    return sum(1 for pl in app.cat_tree.get_children()
               if app.cat_tree.item(pl, "open"))


def _n_open_inner(app):
    """调试用：展开的芯片组数（v2.2.0 起恒为 0 —— 芯片组没有子节点）。"""
    return sum(1 for pl in app.cat_tree.get_children()
               for cs in app.cat_tree.get_children(pl)
               if app.cat_tree.item(cs, "open"))


def a15():
    """A15 折叠：左栏展开状态跨重建保留（v2.2.0 口径）

    三栏重构后左栏只剩「平台 → 芯片组」两级，芯片组是叶子、没有"展开"可言，
    记忆项从「展开过的芯片组 + 折叠过的平台」简化为「折叠过的平台」。
    本段据此改判：**折叠 → 重建后保持折叠；重新展开 → 重建后保持展开**，
    且搜索往返不得污染记忆。
    """
    root, app = _mk_app()
    try:
        pls = app.cat_tree.get_children()
        if not pls:
            ck("A15 前置：目录树非空", False, "无平台节点")
            return
        pl = pls[0]
        pname = app._iid_full[pl][0]
        ck("A15.0 前置：平台默认展开", bool(app.cat_tree.item(pl, "open")), pname)
        # 折叠一个平台 → 重建后应保持折叠
        app.cat_tree.item(pl, open=False)
        _pump(root, 3)
        app._populate_tree()
        _pump(root, 3)
        i2 = _find(app, pname)
        ck("A15.1 折叠平台后重建仍保持折叠",
           bool(i2) and not app.cat_tree.item(i2, "open"),
           "open=%r" % (app.cat_tree.item(i2, "open") if i2 else None))
        # 重新展开 → 重建后保持展开
        app.cat_tree.item(_find(app, pname), open=True)
        _pump(root, 3)
        app._populate_tree()
        _pump(root, 3)
        i3 = _find(app, pname)
        ck("A15.2 重新展开后重建仍保持展开",
           bool(i3) and bool(app.cat_tree.item(i3, "open")),
           "open=%r" % (app.cat_tree.item(i3, "open") if i3 else None))
        # 搜索往返不得污染记忆（搜索态强制展开，但那不该写回记忆）
        app.cat_tree.item(_find(app, pname), open=False)
        _pump(root, 3)
        app._search_q = "her"
        app._populate_tree()
        _pump(root, 3)
        # 搜索态下平台行的计数会变（"Intel 主板（554 款）" → "（N 款）"），
        # 按旧标签 _find 必然落空 —— 改判「所有平台行都展开」。
        _opened = [bool(app.cat_tree.item(p, "open"))
                   for p in app.cat_tree.get_children()]
        ck("A15.2b 搜索态下平台被强制展开（命中项必须看得见）",
           bool(_opened) and all(_opened), "各平台展开态=%r" % (_opened,))
        app._search_q = ""
        app._populate_tree()
        _pump(root, 3)
        i4 = _find(app, pname)
        ck("A15.3 搜索往返后仍保持折叠（搜索态不污染记忆）",
           bool(i4) and not app.cat_tree.item(i4, "open"),
           "open=%r" % (app.cat_tree.item(i4, "open") if i4 else None))
    finally:
        root.destroy()


def a16():
    """A16 折叠：展开全部 / 折叠全部入口（v2.2.0：作用于平台层）"""
    root, app = _mk_app()
    try:
        head = app.btn_cat_refresh.master
        btns = [c for c in head.winfo_children() if isinstance(c, tk.Button)]
        btn = None
        for b in btns:
            t = str(b.cget("text"))
            if "展开" in t or "折叠" in t:
                btn = b
                break
        ck("A16.1 左栏标题行有展开/折叠切换入口", btn is not None,
           "标题行按钮=%r" % ([str(b.cget("text")) for b in btns],))
        if btn is None:
            return
        p("    标题行剩余空间：行宽=%d 已用=%d"
          % (head.winfo_width(), sum(c.winfo_reqwidth() for c in head.winfo_children())))
        n_pl = len(app.cat_tree.get_children())
        ck("A16.2 初始为全展开态、文案是「折叠全部」",
           "折叠" in str(btn.cget("text")) and _n_open_cs(app) == n_pl,
           "%r 展开=%d/%d" % (str(btn.cget("text")), _n_open_cs(app), n_pl))
        btn.invoke()
        _pump(root, 4)
        ck("A16.3 点击后 %d 个平台全部折叠" % n_pl, _n_open_cs(app) == 0,
           "已展开=%d" % _n_open_cs(app))
        ck("A16.4 文案切成「展开全部」", "展开" in str(btn.cget("text")),
           str(btn.cget("text")))
        app._populate_tree()
        _pump(root, 3)
        ck("A16.5 重建后仍全折叠（折叠记忆生效）", _n_open_cs(app) == 0,
           "已展开=%d" % _n_open_cs(app))
        btn.invoke()
        _pump(root, 4)
        ck("A16.6 再点展开到 %d 个平台" % n_pl, _n_open_cs(app) == n_pl,
           "已展开=%d" % _n_open_cs(app))
        ck("A16.7 芯片组始终是叶子（不再有第二级展开态）",
           _n_open_inner(app) == 0, "已展开芯片组=%d" % _n_open_inner(app))
        ck("A16.8 文案切回「折叠全部」", "折叠" in str(btn.cget("text")),
           str(btn.cget("text")))
        app._populate_tree()
        _pump(root, 3)
        ck("A16.9 展开态重建后仍保持展开", _n_open_cs(app) == n_pl,
           "已展开=%d" % _n_open_cs(app))
    finally:
        root.destroy()


def a17():
    """A17 折叠：重建树后滚动位置回位"""
    root, app = _mk_app()
    try:
        for pl in app.cat_tree.get_children()[:2]:
            app.cat_tree.item(pl, open=True)
            for cs in app.cat_tree.get_children(pl)[:8]:
                app.cat_tree.item(cs, open=True)
        _pump(root, 4)
        cap = getattr(app, "_capture_open", None)
        if callable(cap):
            cap()
        app.cat_tree.yview_moveto(0.5)
        _pump(root, 4)
        r0 = app.cat_tree.yview()[0]
        app._populate_tree()
        _pump(root, 4)
        r1 = app.cat_tree.yview()[0]
        ck("A17 重建后滚动位置大致保持（不再跳回顶部）", abs(r1 - r0) < 0.10,
           "%.3f -> %.3f" % (r0, r1))
    finally:
        root.destroy()


A18_DESC = ("1. 修正系统稳定性与超频兼容性问题；2. 改善与部分 USB 设备的兼容性；"
            "3. 更新 Intel Microcode 以提升 CPU 稳定性；4. 修正 RAID 配置在特定硬盘"
            "组合下的识别异常；5. 提升 DDR5 高频内存的兼容性。")


def a18():
    """A18 文字展现：BIOS 列表「更新说明」悬停显示全文"""
    root, app = _mk_app(1200, 780)
    try:
        app.groups = {"BIOS": [
            {"download_version": "2703", "download_release": "2025/01/02",
             "download_size": 14500000, "download_description": A18_DESC},
            {"download_version": "2602", "download_release": "2024/09/11",
             "download_size": 14400000, "download_description": "小幅修正"}]}
        app.badges = ["BIOS"]
        app._load_badge("BIOS")
        _pump(root, 5)
        kids = app.tv.get_children()
        if not kids:
            ck("A18 前置：列表有数据", False, "空列表")
            return
        iid = kids[0]
        shown = str(app.tv.item(iid, "values")[3])
        ck("A18.0 前置：说明确实被列宽截断", len(shown) < len(A18_DESC)
           and shown.endswith("…"), repr(shown[-12:]))
        # 取单元格**水平中心**：bbox 给的是内容区，左边界会落在前一列
        # （实测 bbox x=372 而 identify_column(372)="#3"），贴边取点会测错列。
        def _mid(bb):
            return bb[0] + max(2, bb[2] // 2)

        # 注意：这里**不能**在 _on_tv_motion 之后 _pump —— 真实鼠标此刻可能正压在
        # 列表上，update() 会把真正的 <Motion> 投递进来、当场把浮窗关掉，
        # 断言就会误判成「没弹」。先 pump 让布局落定，再调处理器并立刻读。
        _pump(root, 4)
        bb = app.tv.bbox(iid, "note")
        app._hide_tip()
        app._on_tv_motion(_Ev(_mid(bb), bb[1] + 4, 400, 400))
        ck("A18.1 悬停「更新说明」单元格弹出完整说明", app._tip_txt == A18_DESC,
           "tip=%r" % (str(app._tip_txt)[:44],))
        bv = app.tv.bbox(iid, "ver")
        app._hide_tip()
        app._on_tv_motion(_Ev(_mid(bv), bv[1] + 4, 300, 400))
        ck("A18.2 悬停版本列不弹说明", app._tip_txt is None, "tip=%r" % (app._tip_txt,))
        iid2 = kids[1]
        if str(app.tv.item(iid2, "values")[3]) == "小幅修正":
            bb2 = app.tv.bbox(iid2, "note")
            app._hide_tip()
            app._on_tv_motion(_Ev(_mid(bb2), bb2[1] + 4, 400, 430))
            ck("A18.3 未被截断的短说明不弹（不打扰）", app._tip_txt is None,
               "tip=%r" % (app._tip_txt,))
        # 移出列表 → 浮窗必须收起（否则会一直悬在屏幕上挡视线）
        app._on_tv_motion(_Ev(_mid(bb), bb[1] + 4, 400, 400))
        app._on_tv_leave()
        ck("A18.4 鼠标移出列表后提示自动收起", app._tip_txt is None
           and app._tip is None, "tip=%r" % (app._tip_txt,))
        app._hide_tip()
    finally:
        root.destroy()


def a19():
    """A19 文字展现：主板级提示框 wraplength 跟随窗口宽度"""
    root, app = _mk_app(1400, 800)
    try:
        app._panel_note_box.pack(fill="x")
        app.v_panel_note.set("1. 请先确认主板型号与 BIOS 版本匹配；2. 升级过程中请勿断电或重启；"
                             "3. 建议使用 FAT32 格式的 U 盘并通过 BIOS FlashBack 功能更新；"
                             "4. 升级前请备份当前 BIOS 设置。")
        vals = []
        for w in (1400, 1180, 900):
            root.geometry("%dx%d" % (w, 700))
            _pump(root, 5)
            wl = int(app._panel_note_lbl.cget("wraplength"))
            lw = app._panel_note_lbl.winfo_width()
            vals.append(wl)
            ck("A19 窗口 %d 时换行宽不超标签宽（不裁字）" % w, wl <= lw + m.S(2),
               "wraplength=%d 标签宽=%d" % (wl, lw))
        ck("A19 wraplength 随窗口变化（非常量 720）", len(set(vals)) > 1,
           "取到=%r" % (vals,))
    finally:
        try:
            app._panel_note_box.pack_forget()
        except Exception:
            pass
        root.destroy()


def a20():
    """A20 文字展现：悬停提示浮窗做屏幕边界收拢"""
    root, app = _mk_app()
    try:
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        app._hide_tip()
        app._show_tip(LONG_NAME, sw - 24, sh - 24)
        _pump(root, 4)
        t = app._tip
        ck("A20.1 贴屏幕右下角时不越界",
           t is not None and t.winfo_x() + t.winfo_width() <= sw
           and t.winfo_y() + t.winfo_height() <= sh,
           "tip=%s 屏=%dx%d" % ((t.winfo_x(), t.winfo_y(), t.winfo_width(),
                                 t.winfo_height()) if t else None, sw, sh))
        app._hide_tip()
        app._show_tip(LONG_NAME, 320, 260)
        _pump(root, 4)
        t2 = app._tip
        ck("A20.2 常规位置仍贴鼠标右下（没被挪走）",
           t2 is not None and t2.winfo_x() > 320 and t2.winfo_y() > 260,
           "tip=%s" % ((t2.winfo_x(), t2.winfo_y()) if t2 else None,))
        app._hide_tip()
    finally:
        root.destroy()


def a21():
    """A21 文字展现：状态栏超长文本不挤掉右侧「数据源 · 版本」"""
    root, app = _mk_app(1100, 760)
    try:
        sb = app.lbl_status.master
        right = [c for c in sb.winfo_children()
                 if c not in (app.lbl_status, app.dot_status)]
        rl = right[0] if right else None
        msg = ("已下载完成：C:\\Users\\user\\Desktop\\" + "很长的目录名称示例\\" * 10
               + "ROG-MAXIMUS-Z790-EXTREME-ASUS-2703.zip（13.9 MB，SHA-256 校验通过）")
        app._ui_status(msg, "ok")
        _pump(root, 5)
        ck("A21.1 右侧「数据源 · 版本」未被挤掉",
           rl is None or rl.winfo_width() >= rl.winfo_reqwidth(),
           "右 实/需=%d/%d" % (rl.winfo_width(), rl.winfo_reqwidth()) if rl else "无右侧")
        ck("A21.2 左侧状态文本不超出状态栏",
           app.lbl_status.winfo_reqwidth() <= sb.winfo_width(),
           "左需=%d 状态栏宽=%d" % (app.lbl_status.winfo_reqwidth(), sb.winfo_width()))
        ck("A21.3 完整消息仍保留（悬停可看全文）",
           getattr(app, "_status_full", None) == msg,
           "保留=%r" % (str(getattr(app, "_status_full", None))[:40],))
        app._ui_status("就绪")
    finally:
        root.destroy()


# ================================================================ A22 缩进
def a22():
    """A22 UI：左栏缩进收紧 —— 芯片组更靠左边框（v2.0.4 口径，v2.2.0 只剩两级）

    v2.0.4 修的是「芯片组下的型号（第 2 层）离左边框太远」（37px）。三栏重构后
    型号层搬到中栏，左栏只剩「平台 → 芯片组」，被测层级从 2 层变 1 层，
    但**前缀开销口径不变**：文字起点 = CAT_TREE_LEAD + depth×indent + CAT_IND_SLOT。
    """
    root = tk.Tk()
    try:
        app = m.App(root)
        root.deiconify()
        _pump(root, 4)
        app.catalog = _cat_json()
        app.cfg["show_platforms"] = None
        app.cfg["chip_filter"] = None
        app._search_q = ""
        app._populate_tree()
        _pump(root, 6)
        S, tree = m.S, app.cat_tree

        def txt_x(iid):
            """反推该行**文字**的真实起点（控件坐标）。

            注意 bbox(#0) 给的是 cell 起点，**不含** Tk 给每个 item 预留的
            Treeitem.indicator 槽（实测固定 15px，叶子节点也一样占位）——
            所以不能用 bbox 当文字起点，必须用 identify_element 扫出 'text'。
            """
            bb = tree.bbox(iid, "#0")
            if not bb:
                return None
            x0, y0, w, h = bb
            yc = y0 + max(1, h // 2)
            for x in range(max(0, x0 - 40), x0 + min(w, 60)):
                if tree.identify_element(x, yc) == "text":
                    return x
            return None

        pl = tree.get_children("")[0]
        cs = None
        for c in tree.get_children(pl):
            cs = c
            break
        ck("A22.0 目录里有芯片组行（叶子）", cs is not None)
        if cs is None:
            return
        tree.item(pl, open=True)
        _pump(root, 6)

        xp, xc = txt_x(pl), txt_x(cs)
        ind = S(m.CAT_INDENT)
        old_cs = 2 + 1 * S(10) + 15      # v2.0.3 口径（indent=10）第 1 层的文字起点
        ck("A22.1 各层文字起点可测", None not in (xp, xc),
           "platform=%r chipset=%r" % (xp, xc))
        ck("A22.2 缩进已收紧（S(CAT_INDENT) ≤ 6）", ind <= 6, "S(CAT_INDENT)=%d" % ind)
        if None not in (xp, xc):
            ck("A22.3 缩进对层级仍是线性的（chipset − platform == indent）",
               xc - xp == ind, "%d − %d = %d（indent=%d）" % (xc, xp, xc - xp, ind))
            ck("A22.4 芯片组文字比收紧前更靠左", xc < old_cs,
               "chipset 文字起点=%d（收紧前 %d）" % (xc, old_cs))
            ck("A22.5 层级仍可辨识（相邻层文字起点不同）", xc > xp,
               "p=%d c=%d" % (xp, xc))
        # 平台的展开箭头必须还在（收紧缩进不能把箭头挤没）
        bb = tree.bbox(pl, "#0")
        if bb:
            yc = bb[1] + max(1, bb[3] // 2)
            el = tree.identify_element(bb[0] + 2, yc)
            ck("A22.6 平台的展开箭头仍存在", el == "Treeitem.indicator", "元素=%r" % (el,))

        # 真实口径的硬裁检测：显示文本像素宽 ≤ 列宽 − 真实文字起点
        col = tree.column("#0", "width")
        bad = []
        for iid, meta in app._iid_full.items():
            tx = txt_x(iid)
            if tx is None:
                continue
            shown = tree.item(iid, "text")
            wp = app._text_px(shown)
            if wp > col - tx:
                bad.append((shown, wp, col - tx))
        ck("A22.7 左栏各行不被 Tk 硬裁（真实文字起点口径）", not bad,
           "超宽 %d 行 %r" % (len(bad), bad[:2]))
        # 平台行（最长的是「Intel 主板（554 款）」）必须完整显示
        if None not in (xp,):
            longest = max((app._text_px(meta[0]), meta[0])
                          for meta in app._iid_full.values() if meta[1] == 0)
            ck("A22.8 最长的平台行放得下（左栏宽度按实测需求定）",
               longest[0] + m.CAT_TREE_LEAD + m.CAT_IND_SLOT <= col,
               "需 %dpx（含前缀开销 %d）vs 列宽 %d  %r"
               % (longest[0], m.CAT_TREE_LEAD + m.CAT_IND_SLOT, col, longest[1]))
    finally:
        root.destroy()


# ================================================================ A23 三栏宽度（v2.2.0）
def _three_widths(app):
    return (app._cf.winfo_width(), app._mf.winfo_width(), app._rf.winfo_width())


def _cut_rows(app):
    """当前中栏里带省略号（显示文本 ≠ 完整型号名）的行。"""
    return [app._mod_iid_full[i] for i in app._mod_iid_full
            if app.mod_tree.item(i, "text") != app._mod_iid_full[i]]


def a23():
    """A23 UI：三栏宽度分配（v2.2.0 取代旧的「左栏列宽」系列）

    旧版这里是「左栏取消固定 300px 上限、按需放宽到布局区 45%」的一组断言。
    三栏重构后口径根本变了：**左/中栏宽度是常量，窗口宽度只决定右栏能吃多少**
    —— 所以旧的比例上限断言、左栏 468px 断言、右栏 620px 保底断言都已不存在，
    本段整体改判为「三栏宽度契约」。
    """
    root, app = _mk_app(w=1200, h=780)
    try:
        S = m.S
        cw, mw, rw = _three_widths(app)
        body = app._body_width()
        pad = app._body_pane_pad()
        p("  [诊断] body=%d pad=%d 三栏=%d/%d/%d 型号列可用=%d 截断=%d/%d"
          % (body, pad, cw, mw, rw, app._mod_col - m.CAT_TREE_LEAD - m.CAT_IND_SLOT,
             len(_cut_rows(app)), len(app._mod_iid_full)))

        ck("A23.1 左/中栏宽度按常量落地（不随窗口伸缩）",
           cw <= S(m.PICKER_CHIP_W) + 1 and mw <= S(m.PICKER_MODEL_W) + 1,
           "芯片组=%d(≤%d) 型号=%d(≤%d)"
           % (cw, S(m.PICKER_CHIP_W), mw, S(m.PICKER_MODEL_W)))
        ck("A23.2 右栏吃全部剩余且不小于保底",
           rw >= S(m.PICKER_RIGHT_MIN) - 2, "右栏=%d 保底=%d" % (rw, S(m.PICKER_RIGHT_MIN)))
        ck("A23.3 三栏 + 两处间距 + 容器内边距 == 布局区（不溢出）",
           cw + mw + rw + S(m.PANE_GAP) * 2 + pad <= body + 2,
           "%d+%d+%d+%d+%d vs %d" % (cw, mw, rw, S(m.PANE_GAP) * 2, pad, body))

        ck("A23.4 中栏型号列可用宽足够（≥ PICKER_MODEL_W 的 70%）",
           app._mod_col - m.CAT_TREE_LEAD - m.CAT_IND_SLOT
           >= S(m.PICKER_MODEL_W) * 0.70,
           "可用=%d" % (app._mod_col - m.CAT_TREE_LEAD - m.CAT_IND_SLOT))
        cut = _cut_rows(app)
        ck("A23.5 型号列截断行数 <3%（用户已确认接受省略号 + 悬停全名）",
           len(cut) < len(app._mod_iid_full) * 0.03,
           "%d/%d %r" % (len(cut), len(app._mod_iid_full), cut[:2]))

        # 逐行预算口径：显示文本像素宽 ≤ 列宽 − 前缀开销
        lead = m.CAT_TREE_LEAD + m.CAT_IND_SLOT
        bad = []
        for i, full in app._mod_iid_full.items():
            wpx = app._text_px(app.mod_tree.item(i, "text"))
            if wpx > app._mod_col - lead:
                bad.append((full, wpx, app._mod_col - lead))
        ck("A23.6 中栏无一行超出列宽预算（不会出现 Tk 硬裁）", not bad,
           "超宽 %d 行 %r" % (len(bad), bad[:2]))

        # 底部工具条必须跨三栏（宽度 == 布局区宽）
        ck("A23.7 底部工具条跨三栏（宽度贴合布局区）",
           abs(app._abar.winfo_width() - body) <= S(m.PANE_GAP),
           "工具条=%d 布局区=%d" % (app._abar.winfo_width(), body))
        ck("A23.8 底部工具条同时容纳路径与主按钮",
           app.lnk_dir.winfo_width() >= S(120) and app.btn_dl.winfo_width() >= S(80),
           "路径=%d 主按钮=%d" % (app.lnk_dir.winfo_width(), app.btn_dl.winfo_width()))

        # 幂等：重复分配不改宽度
        before = (_three_widths(app), app._cat_col, app._mod_col)
        app._populate_tree()
        _pump(root, 3)
        after = (_three_widths(app), app._cat_col, app._mod_col)
        ck("A23.9 重复分配幂等（三栏宽度与列宽都不变）", before == after,
           "%r → %r" % (before, after))

        # 窗口放宽 → 右栏变宽，左/中栏不变
        root.geometry("%dx%d" % m._fit_size(2000, 900, 40))
        _pump(root, 10, 0.05)
        cw2, mw2, rw2 = _three_widths(app)
        ck("A23.10 窗口放宽到 2000 时右栏变宽、左中栏不变",
           rw2 > rw and cw2 == cw and mw2 == mw,
           "三栏 %d/%d/%d（原 %d/%d/%d）" % (cw2, mw2, rw2, cw, mw, rw))

        # 窗口收窄到最小 → 仍守三栏契约
        root.geometry("%dx%d" % m._fit_size(1100, 760, 40))
        _pump(root, 10, 0.05)
        cw3, mw3, rw3 = _three_widths(app)
        ck("A23.11 窗口收到 1100 时仍满足右栏保底且三栏不溢出",
           rw3 >= S(m.PICKER_RIGHT_MIN) - 2
           and cw3 + mw3 + rw3 + S(m.PANE_GAP) * 2 + app._body_pane_pad()
           <= app._body_width() + 2,
           "三栏=%d/%d/%d 右栏保底=%d" % (cw3, mw3, rw3, S(m.PICKER_RIGHT_MIN)))
        ck("A23.12 收窄时优先让位给右栏（右栏宽度几乎不动）",
           abs(rw3 - S(m.PICKER_RIGHT_MIN)) <= S(4),
           "右栏=%d（保底 %d）" % (rw3, S(m.PICKER_RIGHT_MIN)))
    finally:
        root.destroy()


# ================================================================ A24 右栏列表列宽（v2.0.5）
_STUB_ROWS = [
    {"download_version": "2503", "download_release": "2026/09/22",
     "download_size": 19314321,
     "download_description": "1. Improve system stability. 2. Update AGESA to 1.2.0.3d. "
                             "3. Fix a rare boot hang with certain DDR5 kits at 6400MHz."},
    {"download_version": "2402", "download_release": "2026/07/15",
     "download_size": 19303849,
     "download_description": "Improve memory compatibility and device stability."},
]


def a24():
    """A24 UI：右栏 BIOS 列表列宽随控件宽度收敛（v2.0.5）。

    触发背景：左栏按需放宽后右栏收到 620px 保底，Tk 的 `stretch` **不会收缩**，
    「更新说明」列被硬裁（末尾连省略号都没有，表头也被切）。
    """
    root, app = _mk_app(w=1200, h=780)
    try:
        S, tv = m.S, app.tv
        app.groups = {"stub": _STUB_ROWS}
        app._load_badge("stub")
        _pump(root, 6, 0.05)
        cols = list(tv["columns"])

        def widths():
            return [int(tv.column(c, "width")) for c in cols]

        w0 = widths()
        p("  [诊断] tv宽=%d 各列=%s 合计=%d 右栏=%d col=%d"
          % (tv.winfo_width(), w0, sum(w0), app._rf.winfo_width(), app._cat_col))
        ck("A24.1 三列固定宽度已落地（ver/date/size）",
           w0[:3] == [S(b) for _c, b in m.TV_COL_BASE],
           "%r vs %r" % (w0[:3], [S(b) for _c, b in m.TV_COL_BASE]))
        ck("A24.2 说明列宽度 = 控件宽 − 固定列 − 占位",
           abs(w0[3] - max(S(m.TV_NOTE_MIN),
                           tv.winfo_width() - S(m.TV_COL_OVER) - sum(w0[:3]))) <= 1,
           "note=%d tv=%d" % (w0[3], tv.winfo_width()))
        # 核心：列宽总和不得超过控件宽度，否则最后一列被硬裁
        ck("A24.3 列宽总和 ≤ 控件宽（说明列不再被硬裁）",
           sum(w0) <= tv.winfo_width() + 1, "合计=%d tv=%d" % (sum(w0), tv.winfo_width()))
        ck("A24.4 说明列不低于保底宽", w0[3] >= S(m.TV_NOTE_MIN) - 1, "note=%d" % w0[3])
        # 渲染层：每行说明的显示文本必须落在预算内
        bad = []
        for iid in tv.get_children():
            vals = tv.item(iid, "values")
            wp = app._text_px(vals[3])
            if wp > app._desc_px():
                bad.append((vals[3], wp, app._desc_px()))
        ck("A24.5 每行说明显示文本都在列宽预算内", not bad,
           "超宽 %d 行 %r" % (len(bad), bad[:2]))
        # 表头文字不能被切
        ck("A24.6 表头「更新说明」放得下", app._text_px("更新说明") <= w0[3],
           "表头 %dpx vs 列宽 %d" % (app._text_px("更新说明"), w0[3]))
        # 窗口拉宽 → 说明列跟着变宽，且仍满足总和约束
        root.geometry("%dx%d" % m._fit_size(1600, 900, 40))
        _pump(root, 10, 0.05)
        w1 = widths()
        ck("A24.7 窗口拉宽后说明列变宽且总和仍受约束",
           w1[3] > w0[3] and sum(w1) <= tv.winfo_width() + 1,
           "note %d→%d 合计=%d tv=%d" % (w0[3], w1[3], sum(w1), tv.winfo_width()))
        # 拉窄 → 说明列回落到保底，不出现负宽
        root.geometry("%dx%d" % m._fit_size(1100, 760, 40))
        _pump(root, 10, 0.05)
        w2 = widths()
        ck("A24.8 窗口拉到最小尺寸时说明列仍 ≥ 保底且不溢出",
           w2[3] >= S(m.TV_NOTE_MIN) - 1 and w2[3] < w1[3],
           "note %d（宽窗 %d）合计=%d tv=%d" % (w2[3], w1[3], sum(w2), tv.winfo_width()))
    finally:
        root.destroy()


def a25():
    """A25 v2.1.0 视觉焕新：三级底面 / 卡片描边 / 样式作用域不越界"""
    root, app = _mk_app(1200, 780)
    try:
        root.attributes("-topmost", True)
        _pump(root, 12, 0.05)
        app.groups = {"s": [
            {"download_version": "2503", "download_release": "2026/09/22",
             "download_size": 19314321, "download_description": "stub latest"},
            {"download_version": "2402", "download_release": "2026/07/15",
             "download_size": 19303849, "download_description": "stub old"}]}
        app.v_board_title.set("ROG STRIX X870E-E")
        app._load_badge("s")
        # 目录树先滚到中段：新滚动条去掉了箭头端帽，若停在第 0 行，滑块本身会正好
        # 压在顶部采样窗口里，A25.8 会误判成"端帽还在"。
        app.cat_tree.yview_moveto(0.45)
        _pump(root, 14, 0.06)

        hwnd = ctypes.windll.user32.GetAncestor(root.winfo_id(), 2)
        img, (ox, oy) = _grab(hwnd)
        at = _px(img, ox, oy)
        px = img.load()
        hx = lambda c: "#%02x%02x%02x" % tuple(c)[:3]

        # ---- 卡片描边：以前配上但没画出来（relief="flat" 等于按背景色描边）
        lf = app._cf
        edge = [hx(at(lf, i, 60)) for i in range(0, 4)]
        ck("A25.1 卡片描边真的画出来了（relief=solid 生效）",
           _near(at(lf, 0, 60), m.C_BORDER), "芯片组卡左边缘 %r" % edge)

        cards = []

        def _walk(w):
            try:
                if w.winfo_class() == "TFrame" and (w.cget("style") or "") == "Card.TFrame":
                    cards.append(w.winfo_name())
            except Exception:
                pass
            for _c in w.winfo_children():
                _walk(_c)
        _walk(root)
        ck("A25.2 卡片描边样式只作用于三张主卡片（内层容器走 CardInner）",
           len(cards) == 3, "实际 %d 个 %r" % (len(cards), cards))

        # 像素层复核：右栏内部的可见横线不该被放大
        rf = app._rf
        xr = rf.winfo_rootx() - ox + 40
        ytop = rf.winfo_rooty() - oy
        lines = [y for y in range(ytop, ytop + rf.winfo_height())
                 if _near(px[xr, y], m.C_BORDER)]
        # v2.2.0：右栏多了一块详情面板（自带 1px highlight 描边），
        # 合法横线从 3 条变 5 条 —— 卡片上下沿 / 表格上下沿 / 详情框上下沿。
        # 判据仍守「不扩散」：6 条以内。
        ck("A25.2b 右栏内部可见横线 ≤ 6 条（卡 / 表 / 详情三块描边）",
           len(lines) <= 6, "实际 %d 条 %r" % (len(lines), lines))

        lb, lc = _lum(_rgb(m.C_BG)), _lum(_rgb(m.C_CARD))
        lt, li = _lum(_rgb(m.C_CARD_TOP)), _lum(_rgb(m.C_INSET))
        ck("A25.3 三级底面亮度单调（画布<面板<抬升条；洼地<面板）",
           lb < lc < lt and li < lc,
           "画布%.3f 洼地%.3f 面板%.3f 抬升条%.3f" % (lb, li, lc, lt))

        tv = app.tv
        ck("A25.4 BIOS 表格体实测为洼地色",
           _near(at(tv, 300, tv.winfo_height() - 20), m.C_INSET),
           "%s vs %s" % (hx(at(tv, 300, tv.winfo_height() - 20)), m.C_INSET))
        ck("A25.5 BIOS 表头带实测为抬升条色",
           _near(at(tv, 300, 6), m.C_CARD_TOP),
           "%s vs %s" % (hx(at(tv, 300, 6)), m.C_CARD_TOP))

        kids = tv.get_children()
        bb = tv.bbox(kids[1], "ver") if len(kids) > 1 else None
        rowc = at(tv, bb[0] + 5, bb[1] + bb[3] // 2) if bb else (0, 0, 0)
        ck("A25.6 普通行底色 = 洼地色（行 tag 没把洼地盖回卡片色）",
           bool(bb) and _near(rowc, m.C_INSET), "%s vs %s" % (hx(rowc), m.C_INSET))

        sbs = [w for w in app.cat_tree.master.winfo_children()
               if w.winfo_class() == "TScrollbar"]
        if not sbs:
            ck("A25.7 滚动条两端无箭头端帽", False, "未找到滚动条")
        else:
            sb = sbs[0]
            hh = sb.winfo_height()
            caps = [at(sb, sb.winfo_width() // 2, d) for d in (2, 5, 9)]
            caps += [at(sb, sb.winfo_width() // 2, hh - 10),
                     at(sb, sb.winfo_width() // 2, hh - 6),
                     at(sb, sb.winfo_width() // 2, hh - 3)]
            bad = [hx(c) for c in caps if _near(c, m.C_SCROLL, 10)]
            ck("A25.7 滚动条两端无箭头端帽（不再与滑块同色）",
               not bad, "宽%d 两端命中滑块色 %r" % (sb.winfo_width(), bad))

        lbl = getattr(app, "lbl_pb", None)
        if lbl is None:
            ck("A25.8 进度条行不出现画布色补丁", False, "没有 lbl_pb")
        else:
            # v2.3.0：进度行空闲时整行收起（见 _pb_idle）。先把行放上屏再采样 ——
            # 这条断言守的是「进度行显示时不能画成画布色」，不是「空闲也得显示」。
            # 行出现后布局会整体上移 25px，旧帧坐标失效，必须重新抓。
            app._pb_show()
            _pump(root, 6, 0.05)
            _img2, (_ox2, _oy2) = _grab(hwnd)
            _at2 = _px(_img2, _ox2, _oy2)
            c = _at2(lbl, lbl.winfo_width() // 2, lbl.winfo_height() // 2)
            app._pb_idle()
            _pump(root, 3)
            ck("A25.8 进度条行不出现画布色补丁（空 ttk.Label 会画错底）",
               _near(c, m.C_CARD), "%s vs 卡片 %s" % (hx(c), m.C_CARD))

        sz = lambda f: abs(f[1])
        ck("A25.9 字号层级严格递减 标题>型号>卡片标题>正文>小字",
           sz(m.FONT_TITLE) > sz(m.FONT_BOARD) > sz(m.FONT_H) > sz(m.FONT_S) > sz(m.FONT_SS),
           "%d>%d>%d>%d>%d" % (sz(m.FONT_TITLE), sz(m.FONT_BOARD), sz(m.FONT_H),
                               sz(m.FONT_S), sz(m.FONT_SS)))

        ck("A25.10 三棵树的行高按各自信息密度分档",
           int(app.st.lookup("Cat.Treeview", "rowheight")) == m.S(m.CAT_TREE_ROWH)
           and int(app.st.lookup("Mod.Treeview", "rowheight")) == m.S(m.MOD_TREE_ROWH)
           and int(app.st.lookup("Treeview", "rowheight")) == m.S(m.TV_ROWH),
           "芯片组 %s / 型号 %s / 表格 %s（期望 %d / %d / %d）"
           % (app.st.lookup("Cat.Treeview", "rowheight"),
              app.st.lookup("Mod.Treeview", "rowheight"),
              app.st.lookup("Treeview", "rowheight"),
              m.S(m.CAT_TREE_ROWH), m.S(m.MOD_TREE_ROWH), m.S(m.TV_ROWH)))
    finally:
        root.destroy()


# ================================================================ A26 三栏 + 详情面板（v2.2.0）
def _cat_hits(cat, q):
    """按 _populate_tree / _populate_models 的口径反算 (命中芯片组数, 命中型号数)。"""
    n_cs = n_bd = 0
    for b in cat.get("brands") or []:
        for cs in b.get("chipsets") or []:
            bs = cs.get("boards") or []
            if q in (cs.get("name") or "").lower():
                n_cs += 1
                n_bd += len(bs)
            else:
                m = [x for x in bs if q in (x.get("title") or "").lower()]
                if m:
                    n_cs += 1
                    n_bd += len(m)
    return n_cs, n_bd


A26_DESC = ("1. Improve system stability. 2. Update AGESA to 1.2.0.3d. "
            "3. Fix a rare boot hang with certain DDR5 kits at 6400MHz. "
            "4. Improve compatibility with next-generation processors and high-density "
            "memory modules. 5. Several security patches including CVE-2026-1234.")


def a26():
    """A26 UI：三栏浏览器式布局 + 选中版本详情面板（v2.2.0 新增能力）"""
    root, app = _mk_app(1200, 780)
    try:
        S = m.S
        tree = app.cat_tree
        pls = tree.get_children()

        # ---- 结构：左栏两级、中栏平铺
        ck("A26.1 左栏只有「平台 → 芯片组」两级，芯片组是叶子",
           bool(pls) and all(not tree.get_children(c)
                             for p in pls for c in tree.get_children(p)),
           "平台 %d 个，芯片组 %d 个" % (len(pls),
                                       sum(len(tree.get_children(p)) for p in pls)))
        ck("A26.2 平台行数 == 目录品牌数",
           len(pls) == len((_cat_json().get("brands") or [])),
           "%d vs %d" % (len(pls), len(_cat_json().get("brands") or [])))
        ck("A26.3 中栏型号是平铺的（全部为叶子）",
           all(not app.mod_tree.get_children(i) for i in app.iid_board),
           "型号 %d 个" % len(app.iid_board))
        ck("A26.4 自动选中首个芯片组 → 中栏即列出该组型号",
           app.cur_chipset is not None and len(app.iid_board) > 0,
           "首组 %r 共 %d 款" % (app.v_scope.get(), len(app.iid_board)))
        app.cat_updated = app.catalog.get("updated") or ""
        app._populate_tree()
        _pump(root, 4)
        ck("A26.5 目录计数与目录数据时间各只出现一处",
           app.v_cat.get() != "" and app.v_cat_upd.get() != "",
           "计数=%r 时间=%r" % (app.v_cat.get(), app.v_cat_upd.get()))
        # v2.3.0：保存目录启动即显示。旧版 v_dir 恒为 ""，只有开始下载时才 set，
        # 用户在下单前完全不知道文件会落到哪 —— 而 cfg.save_dir 其实是已知的。
        ck("A26.22 启动后「保存目录」不留白（有持久目录显示路径，无则给占位说明）",
           bool(app.v_dir.get().strip()),
           "v_dir=%r cfg.save_dir=%r" % (app.v_dir.get(), app.cfg.get("save_dir")))

        # ---- 跨组搜索
        exp_cs, exp_bd = _cat_hits(_cat_json(), "tuf")
        app._search_q = "tuf"
        app._populate_tree()
        _pump(root, 5)
        got_cs = sum(len(tree.get_children(p)) for p in tree.get_children())
        ck("A26.6 跨组搜索：中栏跨芯片组平铺全部命中",
           len(app.iid_board) == exp_bd, "%d vs 目录反算 %d" % (len(app.iid_board), exp_bd))
        ck("A26.7 跨组搜索：左栏只保留含命中的芯片组",
           got_cs == exp_cs and 0 < got_cs < 64,
           "%d vs 反算 %d" % (got_cs, exp_cs))
        ck("A26.8 搜索态计数与范围标注就位",
           app.v_cat.get() == "命中 %d 款" % exp_bd and app.v_scope.get() == "搜索结果",
           "%r / %r" % (app.v_cat.get(), app.v_scope.get()))
        ck("A26.9 命中项标题确实都含关键词",
           all("tuf" in (bd.get("title") or "").lower() for bd in app.iid_board.values()))
        # 无命中 → 出口
        app._search_q = "zzz-no-such-board"
        app._populate_tree()
        _pump(root, 4)
        ck("A26.10 无命中时右栏出口（去官网搜索）显示",
           bool(app._nores.winfo_manager()) and len(app.iid_board) == 0)
        # 清空 → 恢复
        app._search_q = ""
        app._populate_tree()
        _pump(root, 5)
        ck("A26.11 清空搜索后左栏恢复全部芯片组、出口收起",
           sum(len(tree.get_children(p)) for p in tree.get_children()) == 64
           and not app._nores.winfo_manager()
           and app.v_scope.get() != "搜索结果",
           "芯片组 %d 个" % sum(len(tree.get_children(p)) for p in tree.get_children()))

        # ---- 详情面板
        app.groups = {"s": [
            {"download_version": "2503", "download_release": "2026/09/22",
             "download_size": 19314321, "download_url": "https://x/2503.zip",
             "download_sha256": "sha256:" + "ab" * 32,
             "download_description": A26_DESC},
            {"download_version": "2402", "download_release": "2026/07/15",
             "download_size": 19303849, "download_description": "短说明。"}]}
        app.badges = ["s"]
        app._load_badge("s")
        _pump(root, 8, 0.05)
        ck("A26.12 详情面板显示内容态且字段齐全",
           app._det_body.winfo_ismapped()
           and app.v_det_title.get().startswith("版本 ")
           and "·" in app.v_det_meta.get()
           and app.v_det_sha.get().startswith("SHA-256"),
           "%r | %r" % (app.v_det_title.get(), app.v_det_meta.get()))
        # v2.3.0：整串 SHA-256 需要 559px，面板只有 517px，旧版直接硬裁掉尾巴且
        # 没有省略号。现在放不下就走「头…尾」，尾部 8 位必须保留（校验要靠它）。
        _sha_full = app._det_sha_full
        _sha_shown = app.v_det_sha.get()
        _sha_w, _sha_avail = app._ss_px(_sha_shown), app._det_sha_avail()
        # 注意：要跟「前缀 + 完整值」的总长比，不是跟 64 位比值 —— 显示串带着
        # "SHA-256  " 前缀，68 字其实比 64 位还长，比错方向会把"已裁切"判成"没裁切"，
        # 利用率那道判据就永远不会生效（首版就是这个错）。
        _sha_clipped = len(_sha_shown) < len("SHA-256  " + _sha_full)
        ck("A26.23 SHA-256 不溢出，尾部 8 位可见，且裁切后仍吃满 ≥75% 可用宽",
           _sha_shown.endswith(_sha_full[-8:])
           and _sha_w <= _sha_avail
           # 光判「不溢出」会放过「明明有 515px 却只用了 256px」这种过度裁切 ——
           # 本断言的这条利用率判据正是首版漏掉的那个方向。
           and (not _sha_clipped or _sha_w >= _sha_avail * 0.75),
           "显示 %d 字 / 完整 %d 字，宽 %d / 可用 %d%s"
           % (len(_sha_shown), len(_sha_full), _sha_w, _sha_avail,
              "" if not _sha_clipped else "，利用率 %.0f%%"
              % (100.0 * _sha_w / max(1, _sha_avail))))
        # v2.3.0：空闲时不显示空进度槽（12px 灰槽横贯工具条，读作「0% 卡住」）
        _pb_idle_mgr = app.pb.winfo_manager()
        app._pb_show()
        _ui_up = app.pb.winfo_manager()
        app._pb_idle()
        ck("A26.24 进度槽空闲收起、下载时上屏（不再是常驻空槽）",
           not _pb_idle_mgr and bool(_ui_up),
           "空闲=%r 下载中=%r" % (_pb_idle_mgr, _ui_up))

        # v2.3.0：右栏副标题行在「未选型号」时，v_board_sub 为空 —— 而空文本的
        # ttk.Label 不按 style 背景色绘制（渲成画布色 #0a0c10），实测在标题下留
        # 一条 4×24 的深色竖痕。此处锁住「副标题行不得存在空文本 ttk.Label」。
        _empty_ttk = []
        for _c in app._brow_board.winfo_children():
            if _c.winfo_class() != "TLabel":
                continue
            _tvname = str(_c.cget("textvariable") or "")
            _val = str(root.getvar(_tvname)) if _tvname else str(_c.cget("text") or "")
            if not _val.strip():
                _empty_ttk.append(str(_c))
        ck("A26.25 副标题行无空文本 ttk.Label（空 ttk 会渲成画布色竖痕）",
           not _empty_ttk
           and app._lbl_board_sub.winfo_class() == "Label"
           and str(app._lbl_board_sub.cget("bg")).upper().lstrip("#")
           == str(m.C_CARD).upper().lstrip("#"),
           "空 ttk 控件=%r 副标题类=%s bg=%r"
           % (_empty_ttk, app._lbl_board_sub.winfo_class(),
              app._lbl_board_sub.cget("bg")))

        shown = app.v_det_desc.get()
        ck("A26.13 超长更新说明被裁到 %d 行以内（悬停看全文）"
           % m.DETAIL_MAX_LINES,
           shown.endswith("…") and app._det_desc_full
           and len(shown) < len(app._det_desc_full),
           "全文 %d 字 → 显示 %d 字" % (len(app._det_desc_full), len(shown)))
        _kids = app.tv.get_children()
        app.tv.selection_set(_kids[1])      # 第 2 行是「短说明。」
        _pump(root, 5)
        ck("A26.14 切到短说明行时不被裁（不打扰）",
           not app.v_det_desc.get().endswith("…"),
           "当前显示 %r" % (app.v_det_desc.get()[:18],))
        app.tv.selection_set(_kids[0])
        _pump(root, 5)
        ck("A26.15 列表区与详情区高度都不低于各自的保底",
           app.tv.winfo_height() >= S(m.RIGHT_LIST_MIN)
           and app._detail_zone.winfo_height() >= S(m.RIGHT_DETAIL_MIN) - 8,
           "列表 tv=%d 详情=%d" % (app.tv.winfo_height(),
                                  app._detail_zone.winfo_height()))
        # 换行宽必须绑在 _detail_zone（宽由卡片决定）而不是标签自己 ——
        # 绑标签会形成「宽度↔换行高」的布局反馈环（实测同窗口下列表区在
        # 291~309px 抖动，极端一次把列表挤到 1px）。
        _zone_cfg = bool(app._detail_zone.bind("<Configure>"))
        _desc_cfg = bool(app._det_desc.bind("<Configure>"))
        ck("A26.16 更新说明的换行宽绑定在详情面板（切断布局反馈环）",
           _zone_cfg and not _desc_cfg,
           "zone=%r desc=%r" % (_zone_cfg, _desc_cfg))
        # 布局确定性：同一状态连续两次布局结果一致
        h1 = (app.tv.winfo_height(), app._detail_zone.winfo_height())
        _pump(root, 8, 0.05)
        h2 = (app.tv.winfo_height(), app._detail_zone.winfo_height())
        ck("A26.17 右栏两段高度稳定（不随布局次数漂移）", h1 == h2,
           "%r → %r" % (h1, h2))

        # ---- 程序同步选中不得触发重复查询（必须先让 cur_slug 指向中栏真实存在的型号，
        #      否则 _populate_models 根本不会 selection_set，事件不产生 —— 断言空转。
        #      首版就是漏了这一步，M3 反证时没有转红。）
        _b0 = next(iter(app.iid_board.values()))
        app.cur_slug = _b0.get("slug")
        app.cur_board = _b0
        ck("A26.19 前置：cur_slug 指向中栏中真实存在的型号",
           any((bd.get("slug") or "") == app.cur_slug
               for bd in app.iid_board.values()),
           "slug=%r" % (app.cur_slug,))
        calls = []
        real = app._load_board
        app._load_board = lambda b: calls.append((b or {}).get("slug"))
        try:
            for _ in range(2):
                app._populate_models(app.cur_chipset)
                _pump(root, 6, 0.05)      # 让队列里的 <<TreeviewSelect>> 真正送达
        finally:
            app._load_board = real
        ck("A26.19 程序同步选中不触发重复查询（slug 守卫，与事件投递时机无关）",
           not calls, "触发 %r" % (calls,))

        # 空态：详情面板让位给提示（放最后 —— 它会把 cur_slug 清空）
        app._show_empty(True)
        _pump(root, 4)
        ck("A26.18 未选型号时详情区整块收起（不残留旧版本，也不与大空态叠成两个空盒子）",
           not app._det_body.winfo_ismapped()
           and not app._detail_zone.winfo_manager(),
           "body=%r zone_mgr=%r" % (app._det_body.winfo_ismapped(),
                                    app._detail_zone.winfo_manager()))
        # 反向：有型号但还没选版本（查询中 / 无数据）时，详情区必须回来并说明原因
        app._show_detail(None, "正在查询该型号的 BIOS 版本…")
        _pump(root, 4)
        ck("A26.21 有型号时的空态仍显示详情区并给出原因（不能一刀切收起）",
           bool(app._detail_zone.winfo_manager())
           and app._det_empty.winfo_ismapped()
           and not app._det_body.winfo_ismapped(),
           "zone_mgr=%r empty=%r" % (app._detail_zone.winfo_manager(),
                                     app._det_empty.winfo_ismapped()))
        app._show_empty(False)

        # ---- 标题区压缩
        ck("A26.20 标题区已压缩（品牌条 + 标题 ≤ 60px）",
           app._body.winfo_y() <= 60, "正文顶 y=%d" % app._body.winfo_y())
    finally:
        root.destroy()


for _fn in (a1, a2, a3, a4, a6, a7, a8, a9, a10, a11, a12, a13, a14,
            a15, a16, a17, a18, a19, a20, a21, a22, a23, a24, a25, a26):
    section(_fn)

p("")
p("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
if FAIL:
    p("FAILED: " + repr(FAIL))
p("")
p("save_catalog 落盘次数=%d" % len(_WRITES))
for _n, _st in _WRITES[:8]:
    p("  size=%d <- %s" % (_n, _st.replace("\n", " | ")))

# ---- 数据文件无条件还原（本脚本不该改变产品数据）
m.save_catalog = _SAVE_REAL
if _CACHE_BAK is not None and io.open(_CACHE, "rb").read() != _CACHE_BAK:
    io.open(_CACHE, "wb").write(_CACHE_BAK)
    p("已还原 %s" % os.path.basename(_CACHE))
if _CFG0_BAK is not None and io.open(_CFG0, "rb").read() != _CFG0_BAK:
    io.open(_CFG0, "wb").write(_CFG0_BAK)
    p("已还原 %s" % os.path.basename(_CFG0))

io.open(RESULT, "w", encoding="utf-8").write("\n".join(LINES))
print("\n".join(LINES[-8:]))
sys.exit(1 if FAIL else 0)
