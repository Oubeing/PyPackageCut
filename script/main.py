# -*- coding: utf-8 -*-
"""PyPackageCut 主程序（GUI）。

在原有方案基础上完善：
- 进程级精确占用判定（psutil 优先，否则 ctypes 枚举目标进程已加载模块/打开句柄），
  从根本上避免“误删”；
- 支持选择多个目标文件夹（目标项目常分布在 runtime / site-packages 等多处）；
- 运行时监控支持“持续监控直到手动停止”，从容应对 GUI 程序“全部功能走一遍需要时间”；
- 干跑预览 + 一键还原，确保删除准确无误；
- 白名单 v2 精确匹配，减少漏删；
- “未检测到占用即拒绝盲目删除”安全守卫。
"""

import json
import os
import sys
import traceback
from pathlib import Path
from typing import List, Optional

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLineEdit, QProgressBar, QCheckBox, QLabel,
    QMessageBox, QFileDialog, QTextEdit, QSpinBox, QGroupBox, QListWidget,
)
from PyQt5.QtCore import Qt, pyqtSignal, QThread
from PyQt5.QtGui import QTextCursor

import cutcore
from cutcore import Whitelist

# 记录白名单文件路径
WHITE_FILE = Path(__file__).parent.joinpath("white_files.json")
# 默认的白名单文件内容，用于第一次程序运行生成 json 文件
WHITE_DICT = {
    "python": [
        "api-ms-win-core",
        "base_library.zip",
        ".tcl",
        "tclIndex",
        "MSVCP140.dll",
        "cacert.pem",
        "cp936.enc",
        "__init__",
        "python.exe",
        "pythonw.exe",
        "VCRUNTIME140_1.dll",
    ],
    "matplotlib": ["matplotlibrc", ".load-order", "matplotlib.svg"],
    "request": ["msgcat-1.6.1.tm"],
    "plotly": ["plotly.json", "plotly.min.js", "package_data/templates", "favicon.ico"],
    "kaleido": ["kaleido/executable"],
    "pyecharts": ["pyecharts"],
    "pyqtwebengine": [
        "QtWebEngineProcess.exe",
        "icudtl.dat",
        "qtwebengine_devtools_resources.pak",
        "qtwebengine_resources",
        "qt.conf",
    ],
    "streamlit": ["streamlit/static"],
    "trame_vtk": ["static_viewer.html"],
    "python-docx": ["docx/templates"],
    "python-pptx": ["pptx/templates"],
    "setuptools": ["setuptools/_vendor"],
}


class WorkThread(QThread):
    """工作线程：扫描 / 监控 / 预览 / 执行移动。支持多个目标目录。"""
    progress_updated = pyqtSignal(float)
    log_updated = pyqtSignal(str)
    preview_done = pyqtSignal(object)      # dict 摘要
    need_confirm = pyqtSignal(int)         # 0 个占用时需确认的文件数
    finished_signal = pyqtSignal(str)      # 完成信息
    error_signal = pyqtSignal(str)

    def __init__(self, white_list: Whitelist, file_paths: List[Path],
                 only_binary: bool, monitor_seconds: int,
                 mode: str = "preview", force: bool = False,
                 continuous: bool = False, target_name: str = "",
                 method: str = "auto"):
        super().__init__()
        self.white_list = white_list
        self.file_paths = [Path(p) for p in file_paths]  # 多目录
        self.only_binary = only_binary
        self.monitor_seconds = max(0, int(monitor_seconds))
        self.mode = mode
        self.force = force
        self.continuous = continuous
        self.target_name = target_name or ""
        self.method = method
        self.ever_used: set = set()
        self._abort = False          # 用户要彻底取消整个任务
        self._stop_monitor = False   # 仅结束监控、随后继续计划/执行（持续监控模式用）

    # ---- 内部工具 ----
    def _log(self, msg: str):
        self.log_updated.emit(msg)

    def _should_stop(self) -> bool:
        return self._abort or self._stop_monitor

    def abort(self):
        """彻底取消整个任务（时间模式下的“停止”）。"""
        self._abort = True

    def stop_monitoring(self):
        """仅结束监控阶段，随后继续做计划/预览/执行（持续监控模式下的“停止”）。"""
        self._stop_monitor = True

    # ---- 主流程 ----
    def run(self):
        try:
            roots = self.file_paths
            # 逐目录收集候选
            candidates_by_root = {}
            total = 0
            for root in roots:
                c = cutcore.get_file_paths(root, self.only_binary)
                candidates_by_root[root] = c
                total += len(c)
            all_candidates = [p for c in candidates_by_root.values() for p in c]
            self._log("候选文件共 %d 个（分布在 %d 个目录）。"
                      % (total, len(roots)))

            # 探测方式提示
            if cutcore._PSUTIL_AVAILABLE:
                det = "进程级精确（psutil：模块+打开句柄）"
            elif cutcore.precise_available():
                det = "进程级精确（ctypes：加载模块枚举）"
            else:
                det = "保守探测（写打开）"
            self._log("占用判定方式：%s" % det)

            ever_used: set = set()
            if self.continuous:
                interval = 3.0
                self._log("已启用「持续监控」：请在目标程序中逐一操作各功能，"
                          "全部走完后点“停止”，再预览/处理。")
            else:
                interval = max(1.0, self.monitor_seconds / 20.0) if self.monitor_seconds > 0 else 2.0

            cutcore.monitor_usage(
                roots, all_candidates, self.monitor_seconds, interval, ever_used,
                self._should_stop, self._log,
                lambda pct: self.progress_updated.emit(pct * 0.4),
                method=self.method, continuous=self.continuous,
                target_name=self.target_name,
            )
            if self._abort:
                self._log("已取消，未执行任何移动。")
                return

            # 无 psutil 的 ctypes 回退场景：极限模式下对未被模块枚举命中的候选
            # 补做一次保守写打开探测，捕获非映射方式持久打开的数据/资源文件。
            if not cutcore._PSUTIL_AVAILABLE and not self.only_binary:
                cutcore.sweep_probe_remainder(all_candidates, ever_used)

            # 若具备 psutil，列出已识别到的目标进程，便于确认监控确实抓到目标程序
            if cutcore._PSUTIL_AVAILABLE:
                targets = cutcore.describe_targets(roots, self.target_name)
                if targets:
                    self._log("已识别到 %d 个目标进程：%s"
                              % (len(targets), ", ".join(targets[:6])))
                elif ever_used:
                    self._log("已记录被占用文件，但未反查到进程名（不影响判定）。")
                else:
                    self._log("提示：未自动发现目标进程，请确认目标程序已启动"
                              "且正在使用所选目录。")

            # 逐目录规划
            to_move, prot_white, prot_use = [], [], []
            for root, cands in candidates_by_root.items():
                tm, pw, pu = cutcore.plan_deletions(
                    cands, ever_used, self.white_list, root)
                to_move.extend(tm)
                prot_white.extend(pw)
                prot_use.extend(pu)

            nothing_in_use = (len(ever_used) == 0 and total > 0)

            summary = {
                "total": total,
                "root_count": len(roots),
                "to_move": to_move,
                "protected_white": prot_white,
                "protected_usage": prot_use,
                "nothing_in_use": nothing_in_use,
            }

            if self.mode == "preview":
                self.progress_updated.emit(100)
                self.preview_done.emit(summary)
                return

            # execute
            if nothing_in_use and not self.force:
                self.progress_updated.emit(100)
                self.need_confirm.emit(len(to_move))
                return

            if self._abort:
                self._log("已取消，未执行移动。")
                return

            self.ever_used = ever_used
            self._perform_move(candidates_by_root)
        except Exception as e:
            self.error_signal.emit(str(e))

    def _perform_move(self, candidates_by_root):
        try:
            manifests = []
            active = [(r, c) for r, c in candidates_by_root.items() if c]
            n = len(active) if active else 1
            done = 0
            for root, cands in active:
                tm, _, _ = cutcore.plan_deletions(
                    cands, self.ever_used, self.white_list, root)
                if not tm:
                    done += 1
                    continue
                done += 1
                base = (done - 1) / n * 100
                span = 100.0 / n

                def cb(p, base=base, span=span):
                    self.progress_updated.emit(base + p / 100.0 * span)

                man = cutcore.execute_move(tm, root, cb, self._log)
                manifests.append(str(man))
            msg = ("移动完成！各目录清单已生成：\n" + "\n".join(manifests) +
                   "\n如目标程序运行异常，请使用“一键还原”。")
            self.finished_signal.emit(msg)
        except Exception as e:
            self.error_signal.emit(str(e))


class MasterGui(QMainWindow):
    """程序主窗口"""

    def __init__(self):
        super().__init__()
        self.white_dict = None
        self.white_list = None
        self.work_thread: Optional[WorkThread] = None

        self.init_ui()
        self.init_data()

    # ---------------- UI ----------------
    def init_ui(self):
        ver = getattr(cutcore, "__version__", "1.0.0")
        self.setWindowTitle("PyPackageCut v%s - 打包瘦身小程序" % ver)
        self.resize(720, 600)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # 多文件夹选择
        folder_box = QGroupBox("目标文件夹（可多选，例如 runtime 与 site-packages）")
        folder_layout = QVBoxLayout(folder_box)
        self.folders_list = QListWidget()
        folder_layout.addWidget(self.folders_list)
        fl_btn = QHBoxLayout()
        self.add_btn = QPushButton("添加文件夹")
        self.add_btn.clicked.connect(self.add_folder)
        self.remove_btn = QPushButton("移除选中")
        self.remove_btn.clicked.connect(self.remove_folder)
        self.clear_btn = QPushButton("清空")
        self.clear_btn.clicked.connect(lambda: self.folders_list.clear())
        fl_btn.addWidget(self.add_btn)
        fl_btn.addWidget(self.remove_btn)
        fl_btn.addWidget(self.clear_btn)
        folder_layout.addLayout(fl_btn)
        layout.addWidget(folder_box)

        # 选项区
        opt_box = QGroupBox("瘦身选项")
        opt_layout = QHBoxLayout(opt_box)

        self.mode_check = QCheckBox("当前：安全模式（仅 .pyd/.dll）")
        self.mode_check.setChecked(True)
        self.mode_check.stateChanged.connect(self.mode_change)

        monitor_label = QLabel("运行时监控(秒)：")
        self.monitor_spin = QSpinBox()
        self.monitor_spin.setRange(0, 1800)
        self.monitor_spin.setValue(30)
        self.monitor_spin.setToolTip(
            "在用户操作目标程序的窗口期内反复探测占用；"
            "任何“曾被打开过”的文件都保留，可显著降低懒加载误删。0=仅快照。"
        )

        self.continuous_check = QCheckBox("持续监控(手动停止)")
        self.continuous_check.setToolTip(
            "勾选后忽略上面的秒数，持续采样直到你点“停止”。"
            "适合 GUI 程序：先勾选，开始处理/预览后慢慢把每个功能都点一遍，"
            "确认全加载完再点“停止”。"
        )

        opt_layout.addWidget(self.mode_check)
        opt_layout.addStretch(1)
        opt_layout.addWidget(monitor_label)
        opt_layout.addWidget(self.monitor_spin)
        opt_layout.addWidget(self.continuous_check)
        layout.addWidget(opt_box)

        # 目标进程名 + 说明
        tgt_layout = QHBoxLayout()
        tgt_label = QLabel("目标进程名(可选)：")
        self.target_edit = QLineEdit()
        self.target_edit.setPlaceholderText("如 app.exe；留空则自动发现引用所选目录的进程")
        tgt_layout.addWidget(tgt_label)
        tgt_layout.addWidget(self.target_edit, stretch=1)
        layout.addLayout(tgt_layout)

        # 进度
        prog_layout = QHBoxLayout()
        prog_label = QLabel("处理进度")
        prog_label.setAlignment(Qt.AlignCenter)
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        prog_layout.addWidget(prog_label)
        prog_layout.addWidget(self.progress_bar)
        layout.addLayout(prog_layout)

        # 日志
        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setLineWrapMode(QTextEdit.NoWrap)
        layout.addWidget(self.log_edit, stretch=1)

        # 按钮行
        btn_layout = QHBoxLayout()
        self.preview_btn = QPushButton("分析预览")
        self.preview_btn.clicked.connect(self.start_preview)
        self.start_btn = QPushButton("开始处理")
        self.start_btn.clicked.connect(self.start_work)
        self.restore_btn = QPushButton("一键还原(全部目录)")
        self.restore_btn.clicked.connect(self.restore_work)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.clicked.connect(self.stop_work)
        self.stop_btn.setEnabled(False)
        self.info_btn = QPushButton("使用说明")
        self.info_btn.clicked.connect(self.sys_info)

        btn_layout.addWidget(self.preview_btn)
        btn_layout.addWidget(self.start_btn)
        btn_layout.addWidget(self.restore_btn)
        btn_layout.addWidget(self.stop_btn)
        btn_layout.addWidget(self.info_btn)
        layout.addLayout(btn_layout)

    # ---------------- 数据 ----------------
    def init_data(self):
        if Path(WHITE_FILE).exists():
            self._log("读取白名单配置文件：%s" % WHITE_FILE)
            try:
                with open(WHITE_FILE, "r", encoding="utf-8") as f:
                    self.white_dict = json.load(f)
                if not isinstance(self.white_dict, dict):
                    raise ValueError("白名单配置文件格式错误（应为 JSON 对象）")
            except Exception as e:
                self._log("读取白名单失败：%s；将使用内置默认白名单。" % e)
                self.white_dict = WHITE_DICT
                QMessageBox.warning(
                    self, "警告",
                    "白名单配置文件不合法，已回退到内置默认白名单。\n"
                    "请修正 %s 后重启程序。" % WHITE_FILE)
        else:
            self.white_dict = WHITE_DICT
        self.white_list = Whitelist(self.white_dict)

    def closeEvent(self, event):
        if self.work_thread and self.work_thread.isRunning():
            self.work_thread.abort()
            self.work_thread.wait()
        if not Path(WHITE_FILE).exists():
            try:
                with open(WHITE_FILE, "w", encoding="utf-8") as f:
                    f.write(json.dumps(WHITE_DICT, indent=4, ensure_ascii=False))
                self._log("已写入默认白名单：%s" % WHITE_FILE)
            except Exception as e:
                self._log("保存白名单失败：%s" % e)
        event.accept()

    # ---------------- 交互 ----------------
    def _log(self, msg: str):
        self.log_edit.append(msg)
        cursor = self.log_edit.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.log_edit.setTextCursor(cursor)

    def add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择目标文件夹")
        if folder:
            # 去重
            items = [self.folders_list.item(i).text()
                     for i in range(self.folders_list.count())]
            if folder not in items:
                self.folders_list.addItem(folder)

    def remove_folder(self):
        for item in self.folders_list.selectedItems():
            self.folders_list.takeItem(self.folders_list.row(item))

    def _get_roots(self) -> Optional[List[Path]]:
        items = [self.folders_list.item(i).text()
                 for i in range(self.folders_list.count())]
        if not items:
            QMessageBox.warning(self, "警告", "请先添加至少一个目标文件夹")
            return None
        roots = [Path(p) for p in items if Path(p).exists()]
        missing = [p for p in items if not Path(p).exists()]
        if missing:
            QMessageBox.warning(self, "警告",
                                "以下文件夹不存在，已忽略：\n" + "\n".join(missing))
        if not roots:
            return None
        # 去除相互包含（避免重复处理）
        roots_sorted = sorted(roots, key=lambda p: str(p))
        filtered = []
        for r in roots_sorted:
            if any(str(r).lower().startswith(str(o).lower() + os.sep)
                   or str(r).lower() == str(o).lower() for o in filtered):
                self._log("跳过被其他目录包含的重复目录：%s" % r)
                continue
            filtered.append(r)
        # C 盘提示
        for r in filtered:
            if r.drive and "C" in r.drive.upper():
                reply = QMessageBox.question(
                    self, "警告",
                    "检测到目标路径为 C 盘，可能因权限不足导致探测/移动失败。\n"
                    "是否仍要执行？\n待处理文件夹：%s" % r,
                    QMessageBox.Yes | QMessageBox.No,
                )
                if reply == QMessageBox.No:
                    return None
        return filtered

    def mode_change(self, state):
        if state == Qt.Checked:
            self.mode_check.setText("当前：安全模式（仅 .pyd/.dll）")
        else:
            self.mode_check.setText("当前：极限模式（含数据/资源文件，风险更高）")

    def _spawn_thread(self, mode: str, force: bool = False) -> Optional[WorkThread]:
        roots = self._get_roots()
        if roots is None:
            return None
        if self.work_thread and self.work_thread.isRunning():
            QMessageBox.warning(self, "提示", "已有任务正在执行，请先停止。")
            return None
        self.progress_bar.setValue(0)
        self.preview_btn.setEnabled(False)
        self.start_btn.setEnabled(False)
        self.restore_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

        only_binary = self.mode_check.isChecked()
        mon = self.monitor_spin.value()
        continuous = self.continuous_check.isChecked()
        target_name = self.target_edit.text().strip()
        t = WorkThread(self.white_list, roots, only_binary, mon, mode=mode,
                       force=force, continuous=continuous,
                       target_name=target_name, method="auto")
        t.progress_updated.connect(self.update_progress)
        t.log_updated.connect(self._log)
        t.preview_done.connect(self.on_preview_done)
        t.need_confirm.connect(self.on_need_confirm)
        t.finished_signal.connect(self.on_work_finished)
        t.error_signal.connect(self.on_work_error)
        t.finished.connect(self._on_thread_finished)
        self.work_thread = t
        t.start()
        return t

    def start_preview(self):
        self._log("===== 开始分析预览（不会删除任何文件）=====")
        self._spawn_thread("preview")

    def start_work(self):
        self._log("===== 开始处理（将移动未占用文件到 <文件夹>_new）=====")
        self._spawn_thread("execute")

    def restore_work(self):
        roots = self._get_roots()
        if roots is None:
            return
        reply = QMessageBox.question(
            self, "确认",
            "将依据各目录清单把文件从 <文件夹>_new 还原回原处（共 %d 个目录）。是否继续？"
            % len(roots), QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        total_restored = 0
        total_err = 0
        for r in roots:
            restored, errors = cutcore.restore_from_manifest(r, self._log)
            total_restored += restored
            total_err += len(errors)
        QMessageBox.information(
            self, "还原完成", "共还原 %d 个文件。" % total_restored
            + ("" if total_err == 0 else "\n有 %d 个失败，请查看日志。" % total_err)
        )

    def stop_work(self):
        if self.work_thread and self.work_thread.isRunning():
            if self.work_thread.continuous:
                # 持续监控模式：停止监控后继续生成结果（预览/处理）
                self.work_thread.stop_monitoring()
                self._log("已停止监控，正在生成结果，请稍候…")
            else:
                # 时间模式：彻底取消整个任务
                self.work_thread.abort()
                self._log("已请求取消，请稍候…")

    def _on_thread_finished(self):
        # 线程结束（无论正常完成还是被取消）统一恢复按钮状态
        self._enable_buttons(True)
        self.stop_btn.setEnabled(False)

    # ---------------- 线程回调 ----------------
    def update_progress(self, value):
        self.progress_bar.setValue(int(value))

    def on_preview_done(self, summary):
        self._enable_buttons(True)
        self.stop_btn.setEnabled(False)
        total = summary["total"]
        to_move = summary["to_move"]
        pw = summary["protected_white"]
        pu = summary["protected_usage"]
        self._log("---- 预览结果 ----")
        self._log("候选文件：%d（%d 个目录）" % (total, summary["root_count"]))
        self._log("将移动（疑似未使用）：%d" % len(to_move))
        self._log("白名单保护：%d" % len(pw))
        self._log("运行期占用保护：%d" % len(pu))
        if summary["nothing_in_use"]:
            self._log("【警告】未检测到任何文件被目标程序占用！"
                      "可能目标程序未运行，或文件在临时目录(onedir/onefile)，"
                      "或探测方式受限（可尝试以管理员运行）。"
                      "此时“将移动”列表基本等于全部候选文件，盲目处理极易误删，"
                      "请先运行目标程序并开启全部功能后再试。")
        self._log("---- 将移动的文件（前 200 个）----")
        for p in to_move[:200]:
            self._log("  %s" % p)
        if len(to_move) > 200:
            self._log("  …（共 %d 个，已省略）" % len(to_move))
        if not summary["nothing_in_use"]:
            self._log("确认无误后点击“开始处理”。如需排除某些文件，"
                      "请编辑 white_files.json 后重新预览。")

    def on_need_confirm(self, count):
        self._enable_buttons(True)
        self.stop_btn.setEnabled(False)
        reply = QMessageBox.question(
            self, "高风险确认",
            "未检测到目标程序占用任何文件，将移动全部 %d 个候选文件，"
            "极可能导致目标程序无法运行！\n\n"
            "建议：先运行目标程序并开启全部功能，再重新分析。\n"
            "仍要强制移动吗？" % count,
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self._log("用户确认强制移动（高风险）。")
            self._spawn_thread("execute", force=True)
        else:
            self._log("已取消：未执行移动。")

    def on_work_finished(self, msg):
        self._enable_buttons(True)
        self.stop_btn.setEnabled(False)
        self._log(msg)
        QMessageBox.information(self, "提示", msg)

    def on_work_error(self, msg):
        self._enable_buttons(True)
        self.stop_btn.setEnabled(False)
        self._log("错误：%s" % msg)
        QMessageBox.critical(self, "错误", msg)

    def _enable_buttons(self, enable: bool):
        self.preview_btn.setEnabled(enable)
        self.start_btn.setEnabled(enable)
        self.restore_btn.setEnabled(enable)

    # ---------------- 说明 ----------------
    def sys_info(self):
        ver = getattr(cutcore, "__version__", "1.0.0")
        message = (
            "PyPackageCut v%s\n\n" % ver +
            "运行原理：\n"
            "本程序仅适配 Windows。核心思路：目标程序运行时，它真正需要的文件会被系统加载/"
            "锁定；未被使用的文件即为“疑似未使用”，移动到“<文件夹>_new”并可随时还原。\n\n"
            "关键改进（确保准确无误）：\n"
            "1. 进程级精确占用判定：优先用 psutil 枚举目标进程“已加载模块+已打开句柄”，"
            "否则用 ctypes 直接枚举目标进程已加载模块（.pyd/.dll/.py/.pyc/内存映射文件），"
            "直接读取目标程序真正在用的文件，比旧版“能否写打开”的探测精准得多，避免误删；\n"
            "2. 多文件夹：目标项目常分布在 runtime 与 site-packages 等多处，可一次性全部加入；\n"
            "3. 运行时监控 + 持续监控：在您操作目标程序的窗口期内反复采样，任何曾被打开的文件都保留；"
            "GUI 程序功能多、加载慢，可勾选“持续监控(手动停止)”，慢慢走完所有功能后再点停止；\n"
            "4. 分析预览：先列出将要移动的文件，确认后再执行；\n"
            "5. 一键还原：依据各目录《文件移动清单.txt》把文件还原回原处；\n"
            "6. 安全守卫：若未检测到任何文件被占用，会拒绝盲目删除并提示先运行目标程序。\n\n"
            "使用方法：\n"
            "1. 添加目标文件夹（如 runtime、site-packages），可多个；\n"
            "2. 运行目标程序，开启全部功能，使其完全加载；\n"
            "3. 建议勾选“持续监控(手动停止)”（GUI 程序推荐），或把“运行时监控”设为 30~60 秒，"
            "并尽量在此期间操作各功能；可选填目标进程名以精确限定；\n"
            "4. 点击“分析预览”核对将要移动的文件；\n"
            "5. 确认无误后点击“开始处理”；\n"
            "6. 再次运行目标程序验证；若异常，点“一键还原(全部目录)”。\n\n"
            "模式说明：\n"
            "安全模式：仅处理 .pyd/.dll，成功率最高（推荐）；\n"
            "极限模式：处理所有类型（含数据/资源文件），瘦身更彻底但风险更高，请务必配合预览与还原。\n\n"
            "白名单：首次运行生成 white_files.json，记录不移动的文件；"
            "支持子串、glob(*)、目录级匹配，可按需增删。"
        )
        QMessageBox.information(self, "使用说明", message)


def _global_excepthook(exc_type, exc_value, exc_tb):
    """全局异常钩子：未捕获异常时弹窗提示，避免程序直接闪退/崩溃。"""
    msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    try:
        from PyQt5.QtWidgets import QMessageBox
        QMessageBox.critical(None, "程序异常", msg)
    except Exception:
        sys.stderr.write(msg)
    # 注意：不调用原 excepthook，避免 PyQt 在事件循环中直接退出进程


def main():
    sys.excepthook = _global_excepthook
    app = QApplication(sys.argv)
    window = MasterGui()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
