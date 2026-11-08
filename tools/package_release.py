#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PyPackageCut 一键发布打包脚本
================================

把运行依赖（runtime/、site-packages/、PyPackageCut.exe、PyPackageCut.int、script/）
与项目文档打包成一个可直接分发（双击 PyPackageCut.exe 即可运行）的发布目录，
并可进一步压成 `*.zip` 用于上传 GitHub Releases。

用法
----
    # 一键产出：dist/PyPackageCut-v1.0.0/ 目录 + dist/PyPackageCut-v1.0.0.zip
    python tools/package_release.py

    # 只产出目录（不压缩）
    python tools/package_release.py --no-zip

    # 只压缩成 zip（不保留目录）
    python tools/package_release.py --no-dir

    # 自定义输出位置与包名
    python tools/package_release.py --output-dir build/out --name PyPackageCut

    # 先预览会复制哪些内容（不写任何文件）
    python tools/package_release.py --dry-run

    # 重新打包前清空旧产物
    python tools/package_release.py --clean

    # 仅列出将要打包的组件后退出
    python tools/package_release.py --list

说明
----
- 仅依赖标准库，可在任意装有 Python 3.8+ 的环境运行。
- 自动从 script/cutcore.py 或 pyproject.toml 推断版本号。
- 运行产物（white_files.json、*_new/、*_文件移动清单.txt、__pycache__ 等）会被排除，
  保证产出的是“干净的可分发包”，而非混入本机运行残留。
- 任何用户级错误（缺文件、路径不存在等）都会给出清晰提示并以非零码退出，绝不抛出堆栈崩溃。
"""

import argparse
import hashlib
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

# 脚本所在目录（tools/），其上级即项目根目录
HERE = Path(__file__).resolve().parent
DEFAULT_ROOT = HERE.parent

# 必需组件：缺任何一个都视为无法打包
REQUIRED_COMPONENTS = [
    ("file", "PyPackageCut.exe"),   # 引导 exe（双击入口）
    ("file", "PyPackageCut.int"),   # 引导文件（设置路径并 import main）
    ("dir",  "script"),             # 源码目录（.int 会 import main）
    ("dir",  "runtime"),            # 嵌入式 Python 运行时
    ("dir",  "site-packages"),      # 第三方库
]

# 可选组件：缺失只提示，不影响打包
OPTIONAL_COMPONENTS = [
    ("file", "README.md"),
    ("file", "LICENSE"),
    ("file", "CHANGELOG.md"),
]

# 复制目录时忽略的运行产物 / 缓存
IGNORE_PATTERNS = shutil.ignore_patterns(
    "__pycache__",
    "*.pyc",
    "*.pyo",
    "*.pyd.tmp",
    "white_files.json",          # 运行时生成，不应随包分发
    "*_new",                     # 瘦身移动产物目录
    "*_文件移动清单.txt",          # 瘦身清单
    "Thumbs.db",
    ".DS_Store",
)


# --------------------------------------------------------------------------- #
# 版本检测
# --------------------------------------------------------------------------- #

def detect_version(root: Path) -> str:
    """尽可能准确地解析版本号。

    优先顺序：script/cutcore.py 的 __version__ → pyproject.toml → 0.0.0。
    """
    # 1) 尝试从 cutcore.py 读取 __version__
    cutcore_path = root / "script" / "cutcore.py"
    if cutcore_path.is_file():
        try:
            text = cutcore_path.read_text(encoding="utf-8")
            m = re.search(r"^__version__\s*=\s*['\"]([^'\"]+)['\"]", text, re.MULTILINE)
            if m:
                return m.group(1).strip()
        except (OSError, UnicodeDecodeError):
            pass

    # 2) 尝试从 pyproject.toml 解析
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            text = pyproject.read_text(encoding="utf-8")
            m = re.search(r"^\s*version\s*=\s*['\"]([^'\"]+)['\"]", text, re.MULTILINE)
            if m:
                return m.group(1).strip()
        except (OSError, UnicodeDecodeError):
            pass

    return "0.0.0"


# --------------------------------------------------------------------------- #
# 组件校验与清单
# --------------------------------------------------------------------------- #

def gather_components(root: Path):
    """校验组件并返回 (present, missing_required, missing_optional) 列表。

    每个元素是 (kind, relpath: str, abs: Path)。
    """
    present, missing_required, missing_optional = [], [], []

    for kind, name in REQUIRED_COMPONENTS:
        abs_path = root / name
        ok = abs_path.is_dir() if kind == "dir" else abs_path.is_file()
        if ok:
            present.append((kind, name, abs_path))
        else:
            missing_required.append((kind, name, abs_path))

    for kind, name in OPTIONAL_COMPONENTS:
        abs_path = root / name
        ok = abs_path.is_dir() if kind == "dir" else abs_path.is_file()
        if ok:
            present.append((kind, name, abs_path))
        else:
            missing_optional.append((kind, name, abs_path))

    return present, missing_required, missing_optional


def iter_files(component_kind: str, abs_path: Path):
    """生成组件内将被复制的文件（绝对路径）。目录会套用 IGNORE_PATTERNS。"""
    if component_kind == "file":
        yield abs_path
        return

    for dirpath, dirnames, filenames in os.walk(abs_path):
        # 过滤被忽略的目录
        rel_dir = Path(dirpath).relative_to(abs_path)
        kept_dirs = []
        for d in dirnames:
            cand = rel_dir / d
            if _match_ignore(cand):
                continue
            kept_dirs.append(d)
        dirnames[:] = kept_dirs

        for f in filenames:
            cand = rel_dir / f
            if _match_ignore(cand):
                continue
            yield Path(dirpath) / f


def _match_ignore(rel_path: Path) -> bool:
    """判断一个相对路径是否命中忽略规则。"""
    name = rel_path.name
    parts = rel_path.parts
    # 通配 / 扩展名类规则
    if name in {"__pycache__", "white_files.json", "Thumbs.db", ".DS_Store"}:
        return True
    if name.endswith((".pyc", ".pyo", ".pyd.tmp")):
        return True
    if name == "*_new" or name.endswith("_new"):
        return True
    if name.endswith("_文件移动清单.txt"):
        return True
    return False


# --------------------------------------------------------------------------- #
# 复制逻辑
# --------------------------------------------------------------------------- #

def copy_component(kind: str, name: str, abs_src: Path, dest_root: Path) -> int:
    """复制单个组件到 dest_root/name，返回复制的文件数。"""
    dest = dest_root / name

    if kind == "file":
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(abs_src, dest)
        return 1

    # 目录：需清理旧目录（保证幂等）后整树复制
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(abs_src, dest, ignore=IGNORE_PATTERNS, copy_function=shutil.copy2)
    return sum(1 for _ in iter_files(kind, dest))


def build_directory(present, dest_root: Path, clean: bool) -> dict:
    """把 present 组件复制到 dest_root，返回统计信息。"""
    if clean and dest_root.exists():
        shutil.rmtree(dest_root)
    dest_root.mkdir(parents=True, exist_ok=True)

    stats = {"files": 0, "bytes": 0, "components": 0}
    for kind, name, abs_src in present:
        n = copy_component(kind, name, abs_src, dest_root)
        size = sum(
            (p.stat().st_size for p in iter_files(kind, dest_root / name)),
            0,
        )
        stats["files"] += n
        stats["bytes"] += size
        stats["components"] += 1
        print(f"  [OK] {name:<16} {n:>5} 个文件  {_human(size):>10}")
    return stats


def build_zip(present, dest_root: Path, zip_path: Path) -> dict:
    """把 present 组件压缩成 zip。返回统计信息。"""
    stats = {"files": 0, "bytes": 0}
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for kind, name, abs_src in present:
            for f in iter_files(kind, abs_src):
                # 包内相对路径：以组件名为根
                rel = Path(name) / f.relative_to(abs_src) if kind == "dir" else Path(name)
                zf.write(f, rel.as_posix())
                stats["files"] += 1
                try:
                    stats["bytes"] += f.stat().st_size
                except OSError:
                    pass
    return stats


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #

def _human(num_bytes: int) -> str:
    """把字节数转成人类可读字符串。"""
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{num_bytes} B"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _die(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[name-defined]
    print(f"\n✗ 错误：{msg}", file=sys.stderr)
    sys.exit(code)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="package_release.py",
        description="PyPackageCut 一键发布打包脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--root", type=Path, default=DEFAULT_ROOT,
                   help=f"项目根目录（默认：脚本上级目录 {DEFAULT_ROOT}）")
    p.add_argument("--output-dir", "-o", type=Path, default=None,
                   help="输出目录（默认：<root>/dist）")
    p.add_argument("--name", type=str, default=None,
                   help="包名（默认：PyPackageCut-v<版本>）")
    p.add_argument("--no-dir", action="store_true", help="不产出目录，仅压缩 zip")
    p.add_argument("--no-zip", action="store_true", help="不压缩 zip，仅产出目录")
    p.add_argument("--clean", action="store_true", help="打包前清空旧输出")
    p.add_argument("--dry-run", action="store_true", help="只预览，不写任何文件")
    p.add_argument("--list", action="store_true", help="仅列出组件后退出")
    p.add_argument("--no-checksum", action="store_true", help="不计算 zip 的 SHA256")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    root = args.root.resolve()
    if not root.is_dir():
        _die(f"项目根目录不存在：{root}")

    version = detect_version(root)
    bundle_name = args.name or f"PyPackageCut-v{version}"
    output_dir = args.output_dir or (root / "dist")

    print("=" * 64)
    print(f"  PyPackageCut 发布打包  ·  版本 {version}")
    print("=" * 64)
    print(f"  项目根目录 : {root}")
    print(f"  输出目录   : {output_dir}")
    print(f"  包名       : {bundle_name}")
    print("-" * 64)

    # 校验组件
    present, missing_req, missing_opt = gather_components(root)
    if missing_req:
        lines = "\n".join(f"    - {kind} {name} ({abs_})" for kind, name, abs_ in missing_req)
        _die(f"缺少必需组件，无法打包：\n{lines}")

    print("  将打包的组件：")
    for kind, name, _ in present:
        flag = "必需" if (kind, name) in REQUIRED_COMPONENTS else "可选"
        print(f"    [{flag}] {kind:<4} {name}")

    if missing_opt:
        print("  跳过的可选组件（不存在）：")
        for _, name, _ in missing_opt:
            print(f"    - {name}")

    if args.list:
        print("\n（--list 模式，仅列出，不打包）")
        return 0

    if args.dry_run:
        print("-" * 64)
        print("  预览（不写文件）：")
        total = 0
        for kind, name, abs_src in present:
            n = sum(1 for _ in iter_files(kind, abs_src))
            size = sum((p.stat().st_size for p in iter_files(kind, abs_src)), 0)
            total += size
            print(f"    {name:<16} {n:>5} 个文件  {_human(size):>10}")
        print(f"    {'合计':<14} {'':>5}           {_human(total):>10}")
        print("\n（--dry-run 模式，未写入任何文件）")
        return 0

    # 真正打包
    dest_root = output_dir / bundle_name
    print("-" * 64)
    print("  正在复制组件到发布目录 …")
    stats = build_directory(present, dest_root, clean=args.clean)
    print(f"  发布目录完成：{dest_root}")
    print(f"  共 {stats['files']} 个文件，{_human(stats['bytes'])}")

    if not args.no_zip:
        zip_path = output_dir / f"{bundle_name}.zip"
        print("-" * 64)
        print(f"  正在压缩为 zip：{zip_path}")
        zstats = build_zip(present, dest_root, zip_path)
        print(f"  zip 完成：{zstats['files']} 个文件，{_human(zip_path.stat().st_size)}")
        if not args.no_checksum:
            digest = sha256_of(zip_path)
            print(f"  SHA256: {digest}")

    print("=" * 64)
    print("  完成 ✔")
    if not args.no_zip:
        print(f"  → 上传 {output_dir / (bundle_name + '.zip')} 到 GitHub Releases 即可分发。")
    else:
        print(f"  → 将目录 {dest_root} 整体压缩 / 复制即可分发。")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n✗ 已取消。", file=sys.stderr)
        raise SystemExit(130)
    except SystemExit:
        raise
    except Exception as exc:  # 兜底：任何意外都不应抛出堆栈崩溃
        print(f"\n✗ 未预期错误：{exc}", file=sys.stderr)
        raise SystemExit(2)
