#!/usr/bin/env python3
"""gui.py - LuaJIT 反编译 GUI（PyQt6）。

功能：
  * 拖拽 .luac/文件夹 到拖拽区，或用工具栏打开文件/文件夹（批量）；
  * 左侧标签页显示反编译源码与伪汇编，右侧显示日志/错误；
  * 反编译在 QThreadPool 线程池异步执行，界面不卡顿；
  * 拖拽区有高亮视觉反馈；底部进度条显示批量进度；
  * 源码「另存为」按需落盘（预览阶段不写盘）。

依赖：PyQt6（`pip install PyQt6`）。
运行：python gui.py
"""
import os
import sys

from PyQt6.QtCore import Qt, QThreadPool, QRunnable, QObject, pyqtSignal
from PyQt6.QtGui import QFont, QAction
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame, QLabel, QFileDialog,
    QVBoxLayout, QHBoxLayout, QSplitter, QTextEdit, QPlainTextEdit,
    QTabWidget, QProgressBar, QToolBar, QMessageBox,
)

from decompiler_api import decompile_text, DecompileOptions, detect_version, is_supported_file


# --------------------------------------------------------------------------- #
# 异步任务
# --------------------------------------------------------------------------- #
class WorkerSignals(QObject):
    started = pyqtSignal(str)
    finished = pyqtSignal(str, object)      # 文件路径, DecompileResult
    failed = pyqtSignal(str, str)


class DecompileWorker(QRunnable):
    def __init__(self, path_in, options):
        super().__init__()
        self.path_in = path_in
        self.options = options
        self.signals = WorkerSignals()

    def run(self):
        self.signals.started.emit(self.path_in)
        try:
            result = decompile_text(self.path_in, self.options)
            self.signals.finished.emit(self.path_in, result)
        except Exception as e:  # noqa: BLE001 - 线程边界兜底
            self.signals.failed.emit(self.path_in, repr(e))


# --------------------------------------------------------------------------- #
# 拖拽区域
# --------------------------------------------------------------------------- #
class DropArea(QFrame):
    files_dropped = pyqtSignal(list)

    _BASE_STYLE = (
        "DropArea { border: 2px dashed #9aa0a6; border-radius: 8px; background-color: #fafafa; }"
        " QLabel { color: #5f6368; }"
    )
    _ACTIVE_STYLE = (
        "DropArea { border: 2px solid #1a73e8; border-radius: 8px; background-color: #e8f0fe; }"
        " QLabel { color: #1a73e8; }"
    )

    def __init__(self):
        super().__init__()
        self.setObjectName("DropArea")
        self.setAcceptDrops(True)
        self.setMinimumHeight(84)
        self.setStyleSheet(self._BASE_STYLE)
        layout = QVBoxLayout(self)
        self.label = QLabel("拖拽 .luac 文件或文件夹到此处（可多选）进行反编译")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.label)

    def _set_active(self, active):
        self.setStyleSheet(self._ACTIVE_STYLE if active else self._BASE_STYLE)

    @staticmethod
    def _extract_paths(event):
        paths = []
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                local = url.toLocalFile()
                if local:
                    paths.append(local)
        return paths

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._set_active(True)
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self._set_active(False)

    def dropEvent(self, event):
        self._set_active(False)
        paths = self._extract_paths(event)
        if paths:
            event.acceptProposedAction()
            self.files_dropped.emit(paths)


# --------------------------------------------------------------------------- #
# 主窗口
# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LuaJIT 反编译工具")
        self.resize(1180, 760)

        self.pool = QThreadPool.globalInstance()

        self._total = 0
        self._done = 0
        self.current_source = None
        self.current_pseudoasm = None
        self.current_name = None
        self.last_input_dir = os.getcwd()

        self._build_toolbar()
        self._build_central()
        self._build_statusbar()

    # -- 界面搭建 ---------------------------------------------------------- #
    def _build_toolbar(self):
        tb = QToolBar("主工具栏")
        tb.setMovable(False)
        self.addToolBar(tb)

        open_file = QAction("打开文件…", self)
        open_file.triggered.connect(self.on_open_files)
        tb.addAction(open_file)

        open_dir = QAction("打开文件夹…", self)
        open_dir.triggered.connect(self.on_open_folder)
        tb.addAction(open_dir)

        tb.addSeparator()

        self.save_action = QAction("另存为…", self)
        self.save_action.triggered.connect(self.on_save_as)
        self.save_action.setEnabled(False)
        tb.addAction(self.save_action)

        open_out = QAction("打开输出目录", self)
        open_out.triggered.connect(self.on_open_output_dir)
        tb.addAction(open_out)

        tb.addSeparator()

        self.pseudoasm_action = QAction("伪汇编", self)
        self.pseudoasm_action.setCheckable(True)
        self.pseudoasm_action.setChecked(False)
        tb.addAction(self.pseudoasm_action)

        self.catch_action = QAction("容错(catch_asserts)", self)
        self.catch_action.setCheckable(True)
        self.catch_action.setChecked(True)
        tb.addAction(self.catch_action)

        self.nowarp_action = QAction("不反结构化(no_unwarp)", self)
        self.nowarp_action.setCheckable(True)
        tb.addAction(self.nowarp_action)

        self.linenum_action = QAction("函数行号", self)
        self.linenum_action.setCheckable(True)
        tb.addAction(self.linenum_action)

    def _build_central(self):
        central = QWidget()
        root = QVBoxLayout(central)

        self.drop_area = DropArea()
        self.drop_area.files_dropped.connect(self.on_files_dropped)
        root.addWidget(self.drop_area)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.source_view = QTextEdit()
        self.source_view.setReadOnly(True)
        self.source_view.setFont(QFont("Consolas", 10))
        self.source_view.setPlaceholderText("反编译出的 Lua 源码将显示在这里")

        self.asm_view = QTextEdit()
        self.asm_view.setReadOnly(True)
        self.asm_view.setFont(QFont("Consolas", 9))
        self.asm_view.setPlaceholderText("勾选工具栏“伪汇编”后，这里显示反汇编结果")

        self.tabs = QTabWidget()
        self.tabs.addTab(self.source_view, "Lua 源码")
        self.tabs.addTab(self.asm_view, "伪汇编")

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont("Consolas", 9))
        self.log_view.setPlaceholderText("日志 / 错误信息")

        splitter.addWidget(self.tabs)
        splitter.addWidget(self._titled("日志 / 错误", self.log_view))
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        root.addWidget(splitter)

        self.setCentralWidget(central)

    def _build_statusbar(self):
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(220)
        self.progress.setFormat("%v/%m")
        self.statusBar().addPermanentWidget(self.progress)
        self.statusBar().showMessage("就绪")

    @staticmethod
    def _titled(title, widget):
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel(title))
        layout.addWidget(widget)
        return box

    # -- 选项 -------------------------------------------------------------- #
    def _current_options(self):
        return DecompileOptions(
            catch_asserts=self.catch_action.isChecked(),
            no_unwarp=self.nowarp_action.isChecked(),
            with_line_numbers=self.linenum_action.isChecked(),
            output_pseudoasm=self.pseudoasm_action.isChecked(),
        )

    # -- 事件 -------------------------------------------------------------- #
    def log(self, text):
        self.log_view.appendPlainText(text)

    def on_open_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "选择 LuaJIT 字节码文件", self.last_input_dir)
        if paths:
            self.on_files_dropped(paths)

    def on_open_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择文件夹", self.last_input_dir)
        if folder:
            self.on_files_dropped([folder])

    def on_open_output_dir(self):
        target = self.last_input_dir
        if os.path.isdir(target):
            os.startfile(target)  # noqa: S606 - Windows 打开目录

    def on_save_as(self):
        if not self.current_source:
            return
        default = os.path.join(self.last_input_dir, self._suggested_name())
        path, _ = QFileDialog.getSaveFileName(self, "保存反编译结果", default,
                                              "Lua 文件 (*.lua);;所有文件 (*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf8") as f:
                f.write(self.current_source)
            self.last_input_dir = os.path.dirname(path)
            self.log("[saved] %s" % path)
            self.statusBar().showMessage("已保存: %s" % path, 5000)
        except OSError as e:
            QMessageBox.warning(self, "保存失败", str(e))

    def _suggested_name(self):
        if self.current_name:
            return os.path.splitext(os.path.basename(self.current_name))[0] + ".lua"
        return "output.lua"

    def _expand_paths(self, paths):
        """把文件/文件夹展开为受支持的字节码文件列表。"""
        files = []
        for p in paths:
            if os.path.isdir(p):
                for root, _dirs, names in os.walk(p):
                    for n in names:
                        full = os.path.join(root, n)
                        if is_supported_file(full):
                            files.append(full)
            elif os.path.isfile(p):
                files.append(p)
        return files

    def on_files_dropped(self, paths):
        files = self._expand_paths(paths)
        supported = [f for f in files if is_supported_file(f)]
        skipped = len(files) - len(supported)
        if skipped:
            self.log("[skip] %d 个文件不是有效的 LuaJIT 字节码" % skipped)
        if not supported:
            self.log("[warn] 没有可处理的字节码文件")
            return
        if supported:
            self.last_input_dir = os.path.dirname(os.path.abspath(supported[0]))
            self._total += len(supported)
            self.progress.setMaximum(self._total)
            for f in supported:
                self.enqueue(f)

    def enqueue(self, path_in):
        version = detect_version(path_in)
        self.log("[queue] %s (LuaJIT %s)" % (os.path.basename(path_in), version))
        worker = DecompileWorker(path_in, self._current_options())
        worker.signals.started.connect(self.on_started)
        worker.signals.finished.connect(self.on_finished)
        worker.signals.failed.connect(self.on_failed)
        self.pool.start(worker)

    def on_started(self, path):
        self.statusBar().showMessage("正在反编译 %s" % os.path.basename(path))

    def on_finished(self, path, result):
        self._done += 1
        self.progress.setValue(self._done)
        name = os.path.basename(path)
        if result.ok:
            self.current_name = path
            self.current_source = result.source
            self.current_pseudoasm = result.pseudoasm
            self.source_view.setPlainText(result.source or "")
            if result.pseudoasm is not None:
                self.asm_view.setPlainText(result.pseudoasm)
            self.save_action.setEnabled(True)
            self.log("[done] %s (LuaJIT %s)" % (name, result.bytecode_version))
            if result.log.strip():
                self.log(result.log.strip())
        else:
            self.log("[fail] %s: %s" % (name, result.error))
            if result.log.strip():
                self.log(result.log.strip())
        self._flush_status()

    def on_failed(self, path, error):
        self._done += 1
        self.progress.setValue(self._done)
        self.log("[error] %s: %s" % (os.path.basename(path), error))
        self._flush_status()

    def _flush_status(self):
        if self._done >= self._total:
            self.statusBar().showMessage("完成：%d/%d" % (self._done, self._total))
        else:
            self.statusBar().showMessage("进行中：%d/%d" % (self._done, self._total))


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())