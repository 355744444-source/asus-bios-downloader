# -*- coding: utf-8 -*-
"""验证 _settings_apply 在「目录未加载（chip_opts=None）」时的芯片组过滤处理。

三个用例：
  A 目录未加载 + 选「仅显示勾选」+ 原有过滤 ["Z890"] -> 应保留 ["Z890"]
    （旧实现会静默存成 None = 显示全部，用户意图被吞）
  B 目录未加载 + 选「显示全部」          -> 应存 None
  C 目录已加载 + 选「仅显示勾选」+ 勾第 1 项 -> 应存 ["Z890"]（回归）
"""
import importlib.util
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))


def load_mod():
    target = os.environ.get("PROBE_MOD", "asus_bios_checker.py")
    spec = importlib.util.spec_from_file_location(
        "asus_mod2", os.path.join(HERE, target))
    m = importlib.util.module_from_spec(spec)
    sys.modules["asus_mod2"] = m
    spec.loader.exec_module(m)
    return m


class Var:
    def __init__(self, v):
        self._v = v

    def get(self):
        return self._v


class Win:
    def __init__(self):
        self.destroyed = False

    def destroy(self):
        self.destroyed = True


class Lb:
    def __init__(self, sel):
        self._sel = sel

    def curselection(self):
        return self._sel


def build_app(m, cfg):
    app = object.__new__(m.App)          # 不跑 __init__，避开真实 Tk
    app.cfg = dict(cfg)
    app._populate_tree = lambda: None
    app._set_status = lambda *a, **k: None
    app._spawn = lambda *a, **k: None
    return app


def run_case(m, name, cfg, mode, chip_opts, lb, expect):
    saved = {}
    m.save_cfg = lambda c: (saved.update(c), True)[1]
    m.load_catalog_cached = lambda: (None, 0)
    m.messagebox = types.SimpleNamespace(showwarning=lambda *a, **k: None,
                                         showerror=lambda *a, **k: None,
                                         showinfo=lambda *a, **k: None)
    app = build_app(m, cfg)
    plat_names = ["Intel", "AMD"]
    plat_vars = {"Intel": Var(True), "AMD": Var(True)}
    label = m.REFRESH_OPTS[0][1]
    app._settings_apply(Win(), Var(label), plat_vars, plat_names,
                        Var(mode), lb, chip_opts)
    got = saved.get("chip_filter")
    ok = got == expect
    print("%-52s got=%-14r expect=%-14r %s" %
          (name, got, expect, "PASS" if ok else "FAIL"))
    return ok


def main():
    m = load_mod()
    base = {"refresh_days": 7, "show_platforms": None,
            "chip_filter": ["Z890"], "save_dir": None}
    r = []
    r.append(run_case(m, "A 未加载目录 + 仅显示勾选（应保留原过滤）",
                      base, 1, None, None, ["Z890"]))
    r.append(run_case(m, "B 未加载目录 + 显示全部（应清空）",
                      base, 0, None, None, None))
    r.append(run_case(m, "C 已加载目录 + 勾选第 1 项（回归）",
                      dict(base, chip_filter=None), 1,
                      [("Z890", "Z890"), ("X870", "X870")], Lb([0]), ["Z890"]))
    print("\n判定:", "PASS %d/3" % sum(r) if all(r) else "FAIL %d/3" % sum(r))
    return 0 if all(r) else 1


if __name__ == "__main__":
    sys.exit(main())
