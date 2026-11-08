# -*- coding: utf-8 -*-
"""PyPackageCut 核心引擎（与界面解耦，便于复用与测试）。

相比原方案的关键改进：
1. 占用探测升级为「进程级精确判定」：
   - 优先使用 psutil 枚举目标进程已加载的模块(memory_maps)与已打开的文件句柄(open_files)；
   - 若环境无 psutil（如未打包），则通过 ctypes 调用 Windows Toolhelp 直接枚举目标进程
     已加载的模块(.pyd/.dll/.py/.pyc/内存映射文件)。这两种方式都直接读取“目标程序真正
     在用哪些文件”，比原先“能否以写方式打开”的探测精准得多，从根本上避免误删。
   - 支持自动发现目标进程：凡是引用了所选目录内文件的进程，均视为目标；也可选填进程名精确限定。
2. 多目录支持：目标项目往往分布在多个文件夹（如 runtime 与 site-packages），
   引擎对各目录独立扫描/移动/还原，互不干扰。
3. 运行时监控（多次采样，且支持“持续监控直到手动停止”）：在用户操作目标程序的
   窗口期内反复探测占用，任何“曾被占用过”的文件都保留，从根本上降低“懒加载导致误删”。
   针对 GUI 程序“全部功能走一遍需要时间”的痛点，提供持续监控模式，用户可从容操作，
   完毕后再停止并预览/处理。
4. 白名单 v2：支持 glob、目录级保护、文件名级精确匹配，杜绝子串重叠误匹配
   （如 "used.dll" 误命中 "unused.dll"），减少漏删与误删。
5. 干跑(预览) + 清单 + 一键还原：任何残留误删都可无损恢复，确保删除准确无误。
6. 文件遍历修正：包含所有文件（含无扩展名），并排除工具自身文件与 _new 目录。
"""

import os
import time
import shutil
import fnmatch
import ctypes
import ctypes.wintypes
from pathlib import Path
from typing import Callable, List, Optional, Set, Tuple

__version__ = "1.0.0"

# 始终受保护的文件类型：在“极限模式”下也绝不移动，防止破坏 Python 运行环境
ALWAYS_PROTECTED_TYPES = {".py", ".pyc", ".pyi", ".pth", "._pth"}

# 安全模式只处理这两类二进制
BINARY_TYPES = {".pyd", ".dll"}

# 工具自身文件（避免把瘦身工具自己删掉）
OWN_FILES = {
    "pypackagecut.int", "pypackagecut.exe", "cutcore.py",
    "main.py", "main1.py", "white_files.json",
}

# ------------------------- 进程级精确探测（psutil / ctypes） -------------------------
try:
    import psutil  # type: ignore
    _PSUTIL_AVAILABLE = True
except Exception:  # pragma: no cover
    psutil = None
    _PSUTIL_AVAILABLE = False


def precise_available() -> bool:
    """是否具备进程级精确探测能力（psutil 或 Windows ctypes 任一即可）。"""
    return _PSUTIL_AVAILABLE or (os.name == "nt")


def _norm(p: str) -> str:
    """路径归一化：小写并统一为正斜杠，便于跨来源一致比较。"""
    return p.replace("\\", "/").lower()


def _collect_via_psutil(roots: List[Path], target_name: str = "") -> Set[str]:
    """通过 psutil 枚举目标进程已加载模块与已打开文件，返回其“位于 roots 内”的
    绝对路径(小写)集合。"""
    inuse: Set[str] = set()
    root_strs = tuple(_norm(str(r)) for r in roots)
    tl = (target_name or "").lower().strip()

    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            info = proc.info
            name = (info.get("name") or "").lower()
            exe = (info.get("exe") or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        # 若用户指定了进程名，则仅考虑名称/路径匹配的进程
        if tl and (tl not in name and tl not in exe):
            continue
        try:
            maps = proc.memory_maps()
        except (psutil.AccessDenied, psutil.Error, OSError):
            maps = []
        try:
            ofs = proc.open_files()
        except (psutil.AccessDenied, psutil.Error, OSError):
            ofs = []
        collected: List[str] = []
        for m in maps:
            p = _norm(getattr(m, "path", None) or "")
            if p and any(p.startswith(rs) for rs in root_strs):
                collected.append(p)
        for of in ofs:
            p = _norm(getattr(of, "path", None) or "")
            if p and any(p.startswith(rs) for rs in root_strs):
                collected.append(p)
        if collected:
            inuse.update(collected)
    return inuse


# ---- ctypes 实现的 Windows 进程/模块枚举（psutil 缺失时的精确回退） ----
_kernel32 = ctypes.windll.kernel32 if os.name == "nt" else None
_TH32CS_SNAPPROCESS = 0x00000002
_TH32CS_SNAPMODULE = 0x00000008
_TH32CS_SNAPMODULE32 = 0x00000010


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.wintypes.DWORD),
        ("cntUsage", ctypes.wintypes.DWORD),
        ("th32ProcessID", ctypes.wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", ctypes.wintypes.DWORD),
        ("cntThreads", ctypes.wintypes.DWORD),
        ("th32ParentProcessID", ctypes.wintypes.DWORD),
        ("pcPriClassBase", ctypes.wintypes.LONG),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("szExeFile", ctypes.wintypes.CHAR * 260),
    ]


class _MODULEENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.wintypes.DWORD),
        ("th32ModuleID", ctypes.wintypes.DWORD),
        ("th32ProcessID", ctypes.wintypes.DWORD),
        ("GlblcntUsage", ctypes.wintypes.DWORD),
        ("ProccntUsage", ctypes.wintypes.DWORD),
        ("modBaseAddr", ctypes.c_void_p),
        ("modBaseSize", ctypes.wintypes.DWORD),
        ("hModule", ctypes.wintypes.HMODULE),
        ("szModule", ctypes.wintypes.CHAR * 256),
        ("szExePath", ctypes.wintypes.CHAR * 260),
    ]


def _toolhelp_processes() -> List[Tuple[int, str]]:
    """返回 [(pid, exe_path), ...]，通过 Toolhelp 进程快照。"""
    res: List[Tuple[int, str]] = []
    if _kernel32 is None:
        return res
    try:
        h = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    except OSError:
        return res
    if (h & 0xFFFFFFFF) == 0xFFFFFFFF:
        return res
    try:
        pe = _PROCESSENTRY32()
        pe.dwSize = ctypes.sizeof(_PROCESSENTRY32)
        if _kernel32.Process32First(h, ctypes.byref(pe)):
            while True:
                res.append((pe.th32ProcessID,
                            pe.szExeFile.decode("mbcs", "ignore")))
                try:
                    if not _kernel32.Process32Next(h, ctypes.byref(pe)):
                        break
                except OSError:
                    break
    except OSError:
        pass
    finally:
        try:
            _kernel32.CloseHandle(h)
        except OSError:
            pass
    return res


def _toolhelp_modules(pid: int) -> List[str]:
    """返回某进程已加载模块的完整路径列表（通过 Toolhelp 模块快照）。"""
    res: List[str] = []
    if _kernel32 is None:
        return res
    try:
        h = _kernel32.CreateToolhelp32Snapshot(
            _TH32CS_SNAPMODULE | _TH32CS_SNAPMODULE32, pid)
    except OSError:
        return res
    if (h & 0xFFFFFFFF) == 0xFFFFFFFF:
        return res
    try:
        me = _MODULEENTRY32()
        me.dwSize = ctypes.sizeof(_MODULEENTRY32)
        if _kernel32.Module32First(h, ctypes.byref(me)):
            while True:
                res.append(me.szExePath.decode("mbcs", "ignore"))
                try:
                    if not _kernel32.Module32Next(h, ctypes.byref(me)):
                        break
                except OSError:
                    break
    except OSError:
        pass
    finally:
        try:
            _kernel32.CloseHandle(h)
        except OSError:
            pass
    return res


def _collect_via_ctypes(roots: List[Path], target_name: str = "") -> Set[str]:
    """ctypes 回退方案：枚举所有进程已加载模块，收集位于 roots 内的模块路径。
    覆盖 .pyd/.dll/.py/.pyc/内存映射文件——即瘦身最关键的“被加载文件”。
    任一进程/模块枚举失败都会被隔离，不影响其余进程判定。"""
    inuse: Set[str] = set()
    root_strs = tuple(_norm(str(r)) for r in roots)
    tl = (target_name or "").lower().strip()

    for pid, exe in _toolhelp_processes():
        try:
            exe_l = _norm(exe or "")
            if tl:
                # 仅当用户指定进程名时，要求名称/路径匹配
                if tl not in exe_l:
                    continue
            for mod in _toolhelp_modules(pid):
                ml = _norm(mod or "")
                if ml and any(ml.startswith(rs) for rs in root_strs):
                    inuse.add(ml)
        except Exception:
            # 进程可能在快照与枚举之间退出，忽略即可
            continue
    return inuse


def collect_inuse(roots: List[Path], target_name: str = "") -> Set[str]:
    """统一的“当前被占用文件”集合采集（进程级精确）。

    优先 psutil（含模块与打开的文件句柄），否则 ctypes 模块枚举。
    返回位于 roots 内的绝对路径(小写)集合。
    """
    if _PSUTIL_AVAILABLE:
        try:
            return _collect_via_psutil(roots, target_name)
        except Exception:
            # psutil 异常时回退到 ctypes，保证健壮性
            if _kernel32 is not None:
                return _collect_via_ctypes(roots, target_name)
            return set()
    if _kernel32 is not None:
        return _collect_via_ctypes(roots, target_name)
    return set()


# ------------------------- 保守的“写打开”探测（回退/兜底） -------------------------
def is_file_in_use(path: Path) -> bool:
    """保守探测：尝试以读写方式打开文件。

    - 若因共享冲突 / 权限不足失败 -> 视为被占用（保守，不删除）。
    - 若成功打开 -> 未被占用（可移动）。
    任何异常都保守地视为“被占用”，避免误删。
    仅在无法做进程级枚举的环境/场景下作为兜底使用。
    """
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(str(path), flags)
    except OSError:
        return True
    try:
        os.close(fd)
    except OSError:
        pass
    return False


# ------------------------- 白名单 v2 -------------------------
class Whitelist:
    """白名单匹配 v2：支持 glob、目录级保护、文件名级精确匹配。"""

    def __init__(self, white_dict: Optional[dict]):
        self.patterns: List[str] = []
        if white_dict:
            for vals in white_dict.values():
                if isinstance(vals, (list, tuple)):
                    self.patterns.extend(str(v) for v in vals if v)

    def is_protected(self, path: Path, root: Path) -> bool:
        try:
            rel = str(path.relative_to(root)).replace("\\", "/").lower()
        except ValueError:
            rel = ""
        full = str(path).replace("\\", "/").lower()
        parts = full.split("/")
        for pat in self.patterns:
            p = pat.replace("\\", "/").lower().strip()
            if not p:
                continue
            # 1) glob 精确匹配（相对路径或全路径）
            if fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(full, p):
                return True
            # 2) 目录级保护：pattern 形如 a/b/c，保护该目录下的所有内容
            if "/" in p:
                if rel == p or rel.startswith(p + "/") or full.startswith(p + "/"):
                    return True
                continue
            # 3) 文件名级匹配：仅当 pattern 是完整路径片段时（避免
            #    "used.dll" 误匹配 "unused.dll" 这类子串重叠）
            if p in parts:
                return True
            if ("/" + p + "/") in ("/" + full + "/") \
                    or full.startswith(p + "/") or full.endswith("/" + p):
                return True
        return False


# ------------------------- 文件遍历 -------------------------
def _is_own_file(path: Path) -> bool:
    return path.name.lower() in OWN_FILES


def get_file_paths(directory: Path, only_binary: bool) -> List[Path]:
    """遍历目录下所有文件（含无扩展名），返回候选文件列表。

    - only_binary=True（安全模式）：仅 .pyd / .dll
    - only_binary=False（极限模式）：排除始终受保护的类型，其余全部
    同时排除工具自身文件。
    使用 os.walk 并在每个条目上做异常保护，遇到无权限子目录会跳过而不会中断。
    """
    result: List[Path] = []
    directory = Path(directory)
    try:
        for root, _dirs, files in os.walk(str(directory)):
            for name in files:
                try:
                    path = Path(root) / name
                    if _is_own_file(path):
                        continue
                    suffix = path.suffix.lower()
                    if only_binary:
                        if suffix not in BINARY_TYPES:
                            continue
                    else:
                        if suffix in ALWAYS_PROTECTED_TYPES:
                            continue
                    result.append(path)
                except (OSError, PermissionError):
                    # 单个文件不可访问，跳过
                    continue
    except (OSError, PermissionError) as e:
        # 根目录本身不可访问
        raise RuntimeError("无法遍历目录 %s：%s" % (directory, e))
    return result


# ------------------------- 目录 / 清单 路径 -------------------------
def new_dir_for(root: Path) -> Path:
    """计算移动目标目录（与 root 同级的 <root>_new）。"""
    return Path(str(root) + "_new")


# ------------------------- 监控（采样占用） -------------------------
def _monitor_process(roots, duration, interval, ever_used, should_stop,
                     on_log, on_progress, continuous, target_name):
    """进程级精确监控：周期性调用 collect_inuse，把“曾被占用”的文件路径加入 ever_used。"""
    elapsed = 0.0
    while True:
        if should_stop():
            return
        try:
            s = collect_inuse(roots, target_name)
        except Exception as e:  # 单次采样失败不应中断整体
            on_log("精确探测异常（已跳过本次）：%s" % e)
            s = set()
        ever_used.update(s)
        if not continuous:
            elapsed += interval
            on_log("监控中(精确)：已记录 %d 个被占用文件（约 %.0f/%d 秒）"
                   % (len(ever_used), min(elapsed, duration), int(duration)))
            if on_progress:
                on_progress(min(elapsed, duration) / max(duration, 1e-9) * 100)
            if elapsed >= duration:
                return
            time.sleep(interval)
        else:
            on_log("持续监控中(精确)：已记录 %d 个被占用文件（点“停止”结束监控）"
                   % len(ever_used))
            if on_progress:
                on_progress(min(95.0, 50.0 + len(ever_used) % 45))
            time.sleep(interval)


def _monitor_probe(candidates, duration, interval, ever_used, should_stop,
                   on_log, on_progress, continuous):
    """保守的写打开探测监控（仅在无进程级能力时作为兜底）。"""
    elapsed = 0.0
    while True:
        if should_stop():
            return
        for p in candidates:
            if is_file_in_use(p):
                ever_used.add(_norm(str(p)))
        if not continuous:
            elapsed += interval
            on_log("监控中：已记录 %d 个被占用文件（约 %.0f/%d 秒）"
                   % (len(ever_used), min(elapsed, duration), int(duration)))
            if on_progress:
                on_progress(min(elapsed, duration) / max(duration, 1e-9) * 100)
            if elapsed >= duration:
                return
            time.sleep(interval)
        else:
            on_log("持续监控中：已记录 %d 个被占用文件（点“停止”结束监控）"
                   % len(ever_used))
            if on_progress:
                on_progress(min(95.0, 50.0 + len(ever_used) % 45))
            time.sleep(interval)


def monitor_usage(roots, candidates, duration, interval, ever_used, should_stop,
                  on_log, on_progress=None, method="auto", continuous=False,
                  target_name=""):
    """统一的监控入口。

    method:
      - "auto"    : 具备进程级能力则用精确探测，否则回退保守探测
      - "precise" : 强制进程级精确探测
      - "probe"   : 强制保守写打开探测
    continuous: True 时忽略 duration，循环采样直到 should_stop()（适合 GUI 程序慢慢走功能）
    """
    use_precise = (method in ("auto", "precise")) and precise_available()
    if use_precise:
        _monitor_process(roots, duration, interval, ever_used, should_stop,
                         on_log, on_progress, continuous, target_name)
    else:
        _monitor_probe(candidates, duration, interval, ever_used, should_stop,
                       on_log, on_progress, continuous)


def sweep_probe_remainder(candidates: List[Path], ever_used: Set[str]) -> None:
    """对“尚未判定为占用”的候选做一次保守写打开探测，捕获被目标以非映射方式
    持久打开的数据/资源文件（仅在没有 psutil 的 ctypes 回退场景下有意义）。"""
    for p in candidates:
        if _norm(str(p)) in ever_used:
            continue
        if is_file_in_use(p):
            ever_used.add(_norm(str(p)))


# ------------------------- 计划（划分文件） -------------------------
def plan_deletions(
    candidates: List[Path],
    ever_used: Set[str],
    whitelist: Whitelist,
    root: Path,
) -> Tuple[List[Path], List[Path], List[Path]]:
    """依据白名单与占用记录划分文件。

    返回 (to_move, protected_by_white, protected_by_usage)
    """
    to_move: List[Path] = []
    protected_by_white: List[Path] = []
    protected_by_usage: List[Path] = []
    for p in candidates:
        if whitelist.is_protected(p, root):
            protected_by_white.append(p)
            continue
        if _norm(str(p)) in ever_used:
            protected_by_usage.append(p)
            continue
        to_move.append(p)
    return to_move, protected_by_white, protected_by_usage


# ------------------------- 执行移动 / 还原（按目录） -------------------------
def execute_move(
    to_move: List[Path],
    root: Path,
    on_progress: Callable[[float], None],
    on_log: Callable[[str], None],
) -> Path:
    """执行移动，并写入《<目录名>_文件移动清单.txt》。返回清单路径。"""
    new_dir = new_dir_for(root)
    manifest = root.parent.joinpath(f"{root.name}_文件移动清单.txt")
    total = len(to_move) if to_move else 1
    moved: List[str] = []

    for idx, src in enumerate(to_move):
        try:
            rel = src.relative_to(root)
            dst = new_dir.joinpath(rel)
            if dst.exists():
                # 同一文件可能在上一轮残留于目标；先清理再移动，保证幂等
                try:
                    if dst.is_dir():
                        shutil.rmtree(str(dst))
                    else:
                        os.remove(str(dst))
                except OSError as e:
                    on_log("跳过(目标已存在且无法清理) %s -> %s" % (src, e))
                    continue
            if not dst.parent.exists():
                os.makedirs(dst.parent, exist_ok=True)
            # 优先 os.replace（同盘原子覆盖）；跨盘时回退到 shutil.move
            try:
                os.replace(str(src), str(dst))
            except OSError:
                shutil.move(str(src), str(dst))
            moved.append(str(src))
            on_log("移动 %s" % src)
        except Exception as e:  # 单个文件失败不中断整体
            on_log("跳过(移动失败) %s -> %s" % (src, e))

        on_progress((idx + 1) / total * 100)

    # 写清单（覆盖式，保证与本次结果一致）
    try:
        if manifest.exists():
            os.remove(str(manifest))
    except OSError:
        pass
    try:
        with open(manifest, "w", encoding="utf-8") as f:
            for line in moved:
                f.write(line + "\n")
    except PermissionError:
        on_log("权限不足，无法写入清单，请用管理员权限运行。")

    on_log("本次共移动 %d 个文件。" % len(moved))
    return manifest


def restore_from_manifest(
    root: Path,
    on_log: Callable[[str], None],
) -> Tuple[int, List[str]]:
    """根据清单把文件从 <root>_new 还原回原处。返回 (成功数, 错误列表)。"""
    new_dir = new_dir_for(root)
    manifest = root.parent.joinpath(f"{root.name}_文件移动清单.txt")
    if not manifest.exists():
        msg = "未找到清单文件：%s（该目录可能没有可还原的记录）" % manifest
        on_log(msg)
        return 0, [msg]

    with open(manifest, "r", encoding="utf-8") as f:
        lines = [ln.strip() for ln in f if ln.strip()]

    restored = 0
    errors: List[str] = []
    for orig in lines:
        op = Path(orig)
        try:
            rel = op.relative_to(root)
        except ValueError:
            rel = Path(op.name)
        src = new_dir.joinpath(rel)
        if not src.exists():
            continue
        try:
            dst = op
            if not dst.parent.exists():
                os.makedirs(dst.parent, exist_ok=True)
            # 若原处已存在同名文件（例如部分还原残留），先清理再还原
            if dst.exists():
                try:
                    if dst.is_dir():
                        shutil.rmtree(str(dst))
                    else:
                        os.remove(str(dst))
                except OSError as e:
                    errors.append("%s: %s" % (orig, e))
                    on_log("还原失败(目标已存在且无法清理) %s -> %s" % (orig, e))
                    continue
            try:
                os.replace(str(src), str(dst))
            except OSError:
                shutil.move(str(src), str(dst))
            restored += 1
            on_log("还原 %s" % dst)
        except Exception as e:
            errors.append("%s: %s" % (orig, e))
            on_log("还原失败 %s -> %s" % (orig, e))

    on_log("共还原 %d 个文件。" % restored)
    if errors:
        on_log("有 %d 个文件还原失败，请手动检查。" % len(errors))
    return restored, errors


# ------------------------- 便捷：采集某进程引用了哪些目录（诊断用） -------------------------
def describe_targets(roots: List[Path], target_name: str = "") -> List[str]:
    """返回当前引用了 roots 内文件的目标进程描述（pid - name），用于 UI 提示。"""
    inuse = collect_inuse(roots, target_name)
    if not inuse:
        return []
    # 反查：哪些进程的模块/打开文件落在 roots 内
    found: List[str] = []
    if _PSUTIL_AVAILABLE:
        root_strs = tuple(_norm(str(r)) for r in roots)
        tl = (target_name or "").lower().strip()
        seen = set()
        for proc in psutil.process_iter(["pid", "name", "exe"]):
            try:
                info = proc.info
                name = (info.get("name") or "").lower()
                exe = (info.get("exe") or "").lower()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if tl and (tl not in name and tl not in exe):
                continue
            hit = False
            try:
                for m in proc.memory_maps():
                    p = _norm(getattr(m, "path", None) or "")
                    if p and any(p.startswith(rs) for rs in root_strs):
                        hit = True
                        break
            except Exception:
                pass
            if not hit:
                try:
                    for of in proc.open_files():
                        p = _norm(getattr(of, "path", None) or "")
                        if p and any(p.startswith(rs) for rs in root_strs):
                            hit = True
                            break
                except Exception:
                    pass
            if hit:
                key = (info.get("pid"), name)
                if key not in seen:
                    seen.add(key)
                    found.append("%s - %s" % (info.get("pid"), name))
    return found
