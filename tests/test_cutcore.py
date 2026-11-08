# -*- coding: utf-8 -*-
"""PyPackageCut 核心引擎测试。

既可用 pytest 运行：
    pytest
也可直接运行（无需 pytest）：
    python tests/test_cutcore.py
"""

import os
import sys
import tempfile
import stat
import subprocess
import time
from pathlib import Path

# 将 script/ 加入导入路径，使 cutcore 可被导入
_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT_DIR = os.path.abspath(os.path.join(_HERE, "..", "script"))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import cutcore  # noqa: E402


# ------------------------- 白名单 v2 -------------------------
def test_whitelist_substring_no_false_match():
    """used.dll 不应误匹配 unused.dll（子串重叠防护）。"""
    wl = cutcore.Whitelist({"x": ["used.dll"]})
    from pathlib import Path
    root = Path("C:/app")
    assert wl.is_protected(root / "used.dll", root) is True
    assert wl.is_protected(root / "unused.dll", root) is False


def test_whitelist_glob():
    wl = cutcore.Whitelist({"z": ["*.json"]})
    from pathlib import Path
    root = Path("C:/app")
    assert wl.is_protected(root / "keepme.json", root) is True
    assert wl.is_protected(root / "keepme.dll", root) is False


def test_whitelist_directory():
    wl = cutcore.Whitelist({"y": ["pkg"]})
    from pathlib import Path
    root = Path("C:/app")
    assert wl.is_protected(root / "pkg" / "data.txt", root) is True
    assert wl.is_protected(root / "keepme.json", root) is False


# ------------------------- 遍历 / 计划 -------------------------
def test_get_file_paths_includes_extensionless():
    tmp = tempfile.mkdtemp()
    root = os.path.join(tmp, "app")
    os.makedirs(os.path.join(root, "pkg"))
    open(os.path.join(root, "a.pyd"), "wb").write(b"1")
    open(os.path.join(root, "b.dll"), "wb").write(b"2")
    open(os.path.join(root, "readme"), "wb").write(b"3")  # 无扩展名
    from pathlib import Path
    cands = cutcore.get_file_paths(Path(root), only_binary=True)
    names = {p.name for p in cands}
    assert names == {"a.pyd", "b.dll"}
    cands_all = cutcore.get_file_paths(Path(root), only_binary=False)
    assert "readme" in [p.name for p in cands_all]


def test_plan_deletions_respects_usage_and_white():
    tmp = tempfile.mkdtemp()
    root = os.path.join(tmp, "app")
    os.makedirs(root)
    open(os.path.join(root, "used.dll"), "wb").write(b"1")
    open(os.path.join(root, "unused.dll"), "wb").write(b"2")
    open(os.path.join(root, "keep.dll"), "wb").write(b"3")
    from pathlib import Path
    cands = cutcore.get_file_paths(Path(root), only_binary=True)
    wl = cutcore.Whitelist({"t": ["keep.dll"]})
    ever = {cutcore._norm(os.path.join(root, "used.dll"))}
    tm, pw, pu = cutcore.plan_deletions(cands, ever, wl, Path(root))
    # keep.dll 被白名单保护；used.dll 被占用保护；其余进入 to_move
    assert all(p.name != "keep.dll" for p in tm)
    assert all(p.name != "used.dll" for p in tm)
    assert {p.name for p in tm} == {"unused.dll"}


# ------------------------- 移动 / 还原（多目录） -------------------------
def test_execute_and_restore_multiroot():
    tmp = tempfile.mkdtemp()
    r1 = os.path.join(tmp, "runtime")
    r2 = os.path.join(tmp, "site-packages")
    os.makedirs(os.path.join(r1, "pkg"))
    os.makedirs(r2)
    open(os.path.join(r1, "a.pyd"), "wb").write(b"1")
    open(os.path.join(r1, "b.dll"), "wb").write(b"2")
    open(os.path.join(r2, "c.pyd"), "wb").write(b"3")
    open(os.path.join(r2, "d.dll"), "wb").write(b"4")
    from pathlib import Path
    wl = cutcore.Whitelist({"t": []})
    roots = [Path(r1), Path(r2)]
    moved_total = 0
    for r in roots:
        cands = cutcore.get_file_paths(r, only_binary=True)
        tm, _, _ = cutcore.plan_deletions(cands, set(), wl, r)
        man = cutcore.execute_move(tm, r, lambda p: None, lambda m: None)
        nd = cutcore.new_dir_for(r)
        for p in tm:
            assert nd.joinpath(p.relative_to(r)).exists()
        assert len(man.read_text(encoding="utf-8").split()) == len(tm)
        moved_total += len(tm)
    # 还原
    restored_total = 0
    for r in roots:
        res, errs = cutcore.restore_from_manifest(r, lambda m: None)
        restored_total += res
        assert not errs
    assert restored_total == moved_total
    for r in roots:
        for p in cutcore.get_file_paths(r, only_binary=True):
            assert p.exists()


# ------------------------- 占用探测分支 -------------------------
def test_is_file_in_use_branch():
    tmp = tempfile.mkdtemp()
    from pathlib import Path
    used = Path(tmp) / "used.dll"
    unused = Path(tmp) / "unused.dll"
    used.write_bytes(b"1")
    unused.write_bytes(b"2")
    os.chmod(used, stat.S_IREAD)  # 只读 -> 保守视为占用
    try:
        assert cutcore.is_file_in_use(used) is True
        assert cutcore.is_file_in_use(unused) is False
    finally:
        os.chmod(used, stat.S_IWRITE)


# ------------------------- 进程级精确探测（ctypes 真实枚举） -------------------------
def test_collect_inuse_ctypes_real():
    if os.name != "nt":
        # 非 Windows 无 ctypes 回退能力，跳过
        return
    sp = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    time.sleep(1.5)
    try:
        inuse = cutcore.collect_inuse([Path(sys.executable).parent], "")
        key = cutcore._norm(str(Path(sys.executable)))
        assert key in inuse, "应能探测到刚拉起子进程的 python.exe"
    finally:
        sp.terminate()
        try:
            sp.wait(timeout=5)
        except Exception:
            sp.kill()


# ------------------------- psutil 分支逻辑（mock） -------------------------
def test_psutil_branch_mock():
    if cutcore._PSUTIL_AVAILABLE and cutcore.psutil is not None:
        saved_psutil = cutcore.psutil
        saved_flag = True
    else:
        saved_psutil = None
        saved_flag = False

    class FMap:
        def __init__(self, path):
            self.path = path

    class FProc:
        def __init__(self, info, maps, ofs):
            self.info = info
            self._maps = maps
            self._ofs = ofs

        def memory_maps(self):
            return self._maps

        def open_files(self):
            return self._ofs

    class FPsutil:
        def process_iter(self, attrs):
            return iter([
                FProc({"pid": 11, "name": "app.exe", "exe": r"C:\app\app.exe"},
                      [FMap(r"C:\app\runtime\x.pyd"), FMap(r"C:\app\runtime\y.dll")], []),
                FProc({"pid": 12, "name": "other.exe", "exe": r"C:\other\other.exe"},
                      [FMap(r"C:\other\z.dll")], []),
            ])

    from pathlib import Path
    cutcore._PSUTIL_AVAILABLE = True
    cutcore.psutil = FPsutil()
    try:
        res = cutcore._collect_via_psutil([Path(r"C:\app\runtime")], "")
        assert cutcore._norm(r"C:\app\runtime\x.pyd") in res
        assert cutcore._norm(r"C:\app\runtime\y.dll") in res
        assert cutcore._norm(r"C:\other\z.dll") not in res
        # target_name 过滤
        res2 = cutcore._collect_via_psutil([Path(r"C:\app\runtime")], "other")
        assert cutcore._norm(r"C:\app\runtime\x.pyd") not in res2
        # 正斜杠归一化也应命中
        res3 = cutcore._collect_via_psutil([Path(r"C:\app\runtime")], "")
        assert cutcore._norm("C:/app/runtime/y.dll") in res3
    finally:
        cutcore._PSUTIL_AVAILABLE = saved_flag
        cutcore.psutil = saved_psutil


# ------------------------- 独立运行支持 -------------------------
def _run_standalone():
    funcs = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = 0
    failed = 0
    for fn in funcs:
        try:
            fn()
            print("PASS", fn.__name__)
            passed += 1
        except Exception as e:  # noqa: BLE001
            import traceback
            print("FAIL", fn.__name__)
            traceback.print_exc()
            failed += 1
    print("\n==== %d passed, %d failed ====" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_standalone())
