# -*- coding: utf-8 -*-
"""打包态验证（两态验证的第二态）。

1. 解 PYZ 做字符串差集：证明 exe 里的代码就是当前源码（不是旧构建）
2. 版本资源 = 2.0.2.0（onefile 的版本资源读不到 VersionInfo，必须扫 UTF-16-LE 字节）
3. 冷目录运行 --check：断网下不崩、**且一定落自检日志**（这正是 v2.0.2 修的缺陷②）
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(HERE, "dist", "华硕主板BIOS下载工具.exe")
SRC = os.path.join(HERE, "asus_bios_checker.py")
RESULT = os.path.join(HERE, "_v_frozen_result.txt")
FAST = "--fast" in sys.argv          # 只跑 1/2 段（第 3 段要联网跑自检，约 90s）

PASS, FAIL, LINES = [], [], []


def p(s):
    LINES.append(str(s))


def ck(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    p("%s %s %s" % ("[OK]" if cond else "[NG]", name, extra))


p("=== 打包态验证 ===")
p("exe = %s (%d 字节, %s)"
  % (os.path.basename(EXE), os.path.getsize(EXE),
     time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(EXE)))))

# ---------------------------------------------------------------- 1 PYZ 字符串差集
try:
    import marshal
    from PyInstaller.archive.readers import CArchiveReader
    car = CArchiveReader(EXE)
    pyz_name = next(n for n, e in car.toc.items() if e[-1] == "z")
    z = car.open_embedded_archive(pyz_name)
    # ⚠️ 入口脚本**不在 PYZ 里**：它是 CArchive 顶层 typecode='s' 的条目
    # （PYZ 的 247 条全是库模块，没有 asus_bios_checker）。取不到就得会退。
    code = None
    try:
        code = z.extract("asus_bios_checker")
    except Exception:
        code = None
    if code is None:
        code = marshal.loads(car.extract("asus_bios_checker"))
    ck("1.1 能从 exe 里取出主模块字节码", code is not None,
       "PYZ=%s（入口脚本取自 CArchive 's' 条目）" % pyz_name)
except Exception as e:
    p("    归档读取异常: %r" % (e,))
    ck("1.1 能从 exe 里取出主模块字节码", False, repr(e)[:120])
    code = None


def harvest(c, strs, names):
    """递归收集 code 里的字符串常量与标识符名。

    ⚠️ 函数/全局**名字**（_spawn / PX_CACHE_MAX）不在 co_consts 里，它们在 co_names
    与 co_varnames 里；只收 co_consts 会得出「产物不含 _spawn」这种假结论。
    """
    for x in c.co_consts:
        if isinstance(x, str):
            strs.add(x)
        elif isinstance(x, (tuple, frozenset)):
            for y in x:
                if isinstance(y, str):
                    strs.add(y)
        elif hasattr(x, "co_code"):
            harvest(x, strs, names)
    names.update(c.co_names)
    names.update(c.co_varnames)
    return strs, names


if code is not None:
    src = io.open(SRC, encoding="utf-8").read()
    s_str, s_nam = harvest(compile(src, SRC, "exec", dont_inherit=True), set(), set())
    p_str, p_nam = harvest(code, set(), set())
    # 判据一：源码的字符串常量与标识符集合是产物集合的子集（差集为空）
    lost_s = s_str - p_str
    lost_n = s_nam - p_nam
    ck("1.2 产物字符串常量 ⊇ 源码（差集为空）", not lost_s,
       "源码独有 %d 条: %r" % (len(lost_s), sorted(lost_s)[:6]))
    ck("1.2b 产物标识符集合 ⊇ 源码（差集为空）", not lost_n,
       "源码独有 %d 条: %r" % (len(lost_n), sorted(lost_n)[:8]))
    blob = "\n".join(p_str)
    for mark in ("_wrtest_", "PX_CACHE_MAX", "DL_SIZE_TOL", "BOARD_TITLE_GAP",
                 "._wrtest", "save_dir", "cat_busy", "2.3.0"):
        ck("1.3 产物含当前版本标记 %s" % mark, (mark in blob) or (mark in p_nam))
    for mark in ("_remember_dir", "_atomic_write_text", "_tmp_path", "_spawn",
                 "_dir_writable", "_cat_busy", "_set_board_title", "_fit_board_title",
                 "_spawn", "expect_size"):
        ck("1.4 产物含 v2.0.2 标识符 %s" % mark, mark in p_nam)
    # v2.0.3 新增（UI 文字展现 / 折叠）
    for mark in ("_capture_open", "_toggle_all_cs", "_sync_cat_all_btn", "_row_desc",
                 "_on_tv_leave", "_refit_status", "_status_tip",
                 "_on_note_box_configure", "_closed_pl",
                 "_built_q", "_skip_capture", "展开全部", "折叠全部"):
        ck("1.4b 产物含 v2.0.3 标识符 %s" % mark,
           (mark in p_nam) or (mark in blob))
    # v2.0.4 新增（目录树缩进收紧：调用方一律走 LEAD + SLOT 这套前缀开销）
    for mark in ("CAT_TREE_LEAD", "CAT_IND_SLOT"):
        ck("1.4c 产物含 v2.0.4 标识符 %s" % mark,
           (mark in p_nam) or (mark in blob))
    # v2.0.5 新增（左栏不截断：固定上限 → 比例上限 + 布局区内边距实测 + 列表列宽收敛）
    for mark in ("CAT_COL_MARGIN", "CAT_BODY_PAD",
                 "TV_COL_BASE", "TV_NOTE_MIN", "TV_COL_OVER",
                 "_body_pane_pad", "_layout_tv_columns", "_body_pad", "_laying_cols"):
        ck("1.4d 产物含 v2.0.5 标识符 %s" % mark,
           (mark in p_nam) or (mark in blob))
    # v2.1.0 标识符（视觉焕新留下的：三级底面 / 卡片描边作用域 / 进度条标签）
    for mark in ("CardInner.TFrame", "C_CARD_TOP", "C_HAIR", "lbl_pb"):
        ck("1.4e 产物含 v2.1.0 标识符 %s" % mark,
           (mark in p_nam) or (mark in blob))
    # v2.2.0 新增（三栏浏览器式布局 + 选中版本详情面板 + 跨栏底部工具条）
    for mark in ("PICKER_CHIP_W", "PICKER_MODEL_W", "PICKER_RIGHT_MIN",
                 "PANE_GAP", "Mod.Treeview", "MOD_TREE_ROWH",
                 "ActionBar.TFrame", "DETAIL_MAX_LINES",
                 "_layout_panes", "_populate_models", "_show_detail",
                 "_chip_col_need", "_clip_desc", "_detail_zone",
                 "mod_tree", "lnk_dir", "cur_chipset", "_chips_visible"):
        ck("1.4f 产物含 v2.2.0 标识符 %s" % mark,
           (mark in p_nam) or (mark in blob))
    # v2.2.0 移除的旧标识符必须真的不在（否则说明打包的是旧代码）
    # 只看**标识符名**（co_names / co_varnames）：旧名合法地留在新版函数的
    # docstring 里（如 _chip_col_need 的"与旧版 _cat_col_need 的差别"），
    # 拿字符串常量判会误报。
    for gone in ("CAT_TREE_MAX_RATIO", "CAT_CARD_OVER", "CAT_RIGHT_MIN",
                 "_auto_fit_cat", "_cat_col_need", "_lf"):
        ck("1.4g 产物已无旧标识符 %s（按名字判）" % gone, gone not in p_nam)
    # 版本常量判据：要判「孤立的版本字符串常量」而非子串 —— 文档字符串里
    # 会合法地出现 "v2.0.1"~"v2.0.4"（记录历次改动），拿子串判会误报
    ck("1.5 产物版本常量 = 2.3.0", "2.3.0" in p_str and "2.3.0" in s_str)
    for old in ("2.0.1", "2.0.2", "2.0.3", "2.0.4", "2.0.5", "2.1.0", "2.2.0", "2.2.1"):
        ck("1.5b 产物无孤立的 %s 版本常量" % old, old not in p_str,
           "%s 出现在: %r" % (old, [x for x in p_str if x.strip() == old][:3]))
    ck("1.6 源码已无旧固定临时名写法", '_cfg_path() + ".tmp"' not in src)
    ck("1.7 源码已无「顶层 topmost 在 geometry 之后」写法",
       'wm_geometry("+%d+%d" % (px, py))\n            try:\n                tw.attributes("-topmost", True)'
       not in src)
    # v2.0.4：旧缩进常量必须清干净（语义与实测相反，留着会被后人误用）
    ck("1.8 源码已无旧缩进常量 CAT_TREE_PAD / CAT_IND_PAD",
       "CAT_TREE_PAD" not in src and "CAT_IND_PAD" not in src)
    # v2.2.0：中栏型号列表（_populate_models）也用同一套口径 → 3 处变 4 处
    ck("1.9 源码各处前缀开销口径统一（LEAD + SLOT，≥4 处）",
       src.count("CAT_TREE_LEAD + CAT_IND_SLOT") >= 4,
       "出现 %d 次" % src.count("CAT_TREE_LEAD + CAT_IND_SLOT"))
    # v2.0.5：旧的固定列宽上限必须真的删干净。
    # ⚠️ 不能用 `count("CAT_TREE_MAX") == count("CAT_TREE_MAX_RATIO")` ——
    # 注释/文档字符串里**合法地**会提到旧名（「取代固定 CAT_TREE_MAX=300」），
    # 那样算必然多出 1 次，是假失败。要判的是「定义」与「旧用法」都不在。
    ck("1.10 源码已无旧列宽口径（比例上限 / 卡片开销 / 右栏保底 / 初值上限）",
       "CAT_TREE_MAX_RATIO =" not in src and "CAT_CARD_OVER =" not in src
       and "CAT_RIGHT_MIN =" not in src and "CAT_TREE_MIN =" not in src
       and "CAT_TREE_INIT =" not in src and "PICKER_CHIP_W =" in src,
       "定义残留：比例上限=%s 卡片开销=%s 右栏保底=%s 初值=%s"
       % ("CAT_TREE_MAX_RATIO =" in src, "CAT_CARD_OVER =" in src,
          "CAT_RIGHT_MIN =" in src, "CAT_TREE_INIT =" in src))
    # v2.1.0：卡片描边修复必须真的进了产物 —— 判据盯「CardInner 样式存在」且
    # 两张主卡片用的是 solid 描边。旧版是 relief="flat"，borderwidth 照留但画不出边。
    ck("1.11 产物含卡片描边修复（CardInner + relief=solid）",
       "CardInner.TFrame" in src and 'relief="solid", bordercolor=C_BORDER' in src,
       "CardInner=%s solid=%s" % ("CardInner.TFrame" in src,
                                  'relief="solid", bordercolor=C_BORDER' in src))
    ck("1.12 源码内层容器无一处直用 Card.TFrame（防边框扩散）",
       src.count('style="Card.TFrame"') == 1,
       "直用 %d 处（应只剩 _card() 那一处）" % src.count('style="Card.TFrame"'))

# ---------------------------------------------------------------- 2 版本资源
raw = io.open(EXE, "rb").read()
for i, (s, label) in enumerate((("2.3.0.0", "FileVersion/ProductVersion"),
                                ("ASUS_MB_BIOS_Downloader", "InternalName"),
                                ("江苏振远智能科技有限公司", "CompanyName")), 1):
    ck("2.%d 版本资源含 %s" % (i, label), s.encode("utf-16-le") in raw,
       "%s=%s" % (label, s))

# ---------------------------------------------------------------- 3 冷目录 --check
if FAST:
    p("")
    p("（--fast：跳过第 3 段冷目录自检）")
else:
    cold = tempfile.mkdtemp(prefix="_v_cold_")
    try:
        dst_exe = os.path.join(cold, os.path.basename(EXE))
        shutil.copy2(EXE, dst_exe)
        t0 = time.time()
        try:
            r = subprocess.run([dst_exe, "--check"], capture_output=True, timeout=300)
            rc, out, err = r.returncode, r.stdout, r.stderr
        except subprocess.TimeoutExpired:
            rc, out, err = None, b"", b"timeout"
        dt = time.time() - t0
        ck("3.1 冷目录 --check 正常退出", rc == 0, "rc=%r 用时=%.1fs" % (rc, dt))
        log = os.path.join(cold, "asus_bios_checker_selftest.log")
        ck("3.2 自检日志落在 exe 所在目录（_app_dir 正确）", os.path.isfile(log),
           "日志存在=%s" % os.path.isfile(log))
        if os.path.isfile(log):
            txt = io.open(log, encoding="utf-8", errors="replace").read()
            ck("3.3 写出完整自检日志（以「自检结束」收尾）",
               len(txt) > 200 and "自检结束" in txt, "len=%d" % len(txt))
            # 断网时要有失败说明、联网时要有成功计数 —— 两者都算「有明确结论」，
            # 关键是不能像旧版那样一崩就啥都没有
            concluded = any(k in txt for k in ("异常", "失败", "总型号:", "HTTP 206"))
            ck("3.4 日志有明确结论（成功计数或失败说明）", concluded, repr(txt[-70:]))
            p("    ---- 自检日志（%d 字节）----" % len(txt))
            allln = txt.splitlines()
            for ln in allln[:14]:
                p("    " + ln)
            if len(allln) > 22:
                p("    ... （略）")
            for ln in allln[-8:]:
                p("    " + ln)
        ck("3.5 冷目录没有崩溃对话框残留", b"Traceback" not in (err or b""),
           repr((err or b"")[:80]))
        if os.path.isfile(log):
            txt = io.open(log, encoding="utf-8", errors="replace").read()
            # v2.0.3：自检新增配置持久化段 —— 打包态也能一句话回答「设置存哪、能不能写」
            ck("3.6 自检含「配置持久化」段（v2.0.3 新增）", "[0] 配置持久化" in txt,
               "含段标题=%s" % ("[0] 配置持久化" in txt))
            ck("3.7 自检报告配置可正常读写（冷目录下）", "配置可正常读写" in txt,
               repr([ln for ln in txt.splitlines() if "结论" in ln][:1]))
            ck("3.8 自检报告的配置路径就在 exe 所在目录",
               cold in txt.replace("/", "\\"),
               repr([ln for ln in txt.splitlines() if "配置文件" in ln][:1]))
    finally:
        shutil.rmtree(cold, ignore_errors=True)

p("")
p("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
if FAIL:
    p("FAILED: " + repr(FAIL))
io.open(RESULT, "w", encoding="utf-8").write("\n".join(LINES))
print("\n".join(LINES))
sys.exit(1 if FAIL else 0)
