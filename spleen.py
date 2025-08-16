"""Spleen file manager.

Provides a lightweight tabbed interface with search, context menu file
operations, drive monitoring, configurable defaults and a zoomable UI.
"""

import os
import sys
import shutil
import zipfile
import fnmatch
from pathlib import Path
import subprocess

from PyQt5.QtCore import (
    Qt,
    QUrl,
    QSortFilterProxyModel,
    QTimer,
    QStorageInfo,
    pyqtSignal,
    QObject,
    QSettings,
    QRunnable,
    QThreadPool,
    QItemSelectionModel,
)
from PyQt5.QtGui import QDesktopServices, QFont
from PyQt5.QtWidgets import (
    QApplication,
    QTreeView,
    QFileSystemModel,
    QAbstractItemView,
    QMainWindow,
    QTabWidget,
    QWidget,
    QVBoxLayout,
    QLineEdit,
    QMenu,
    QAction,
    QFileDialog,
    QInputDialog,
    QMessageBox,
    QHBoxLayout,
    QProgressDialog,
    QToolButton,
    QCheckBox,
    QListWidget,
)

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


class DirectoryWatcher(QObject, FileSystemEventHandler):
    """Watch a directory using watchdog and notify via Qt signal."""

    changed = pyqtSignal()

    def __init__(self, path: str):
        super().__init__()
        self._path = path
        self._observer = Observer()

    def start(self):
        self._observer.schedule(self, self._path, recursive=True)
        self._observer.start()

    def stop(self):
        self._observer.stop()
        self._observer.join()

    # watchdog callback
    def on_any_event(self, event):  # type: ignore[override]
        self.changed.emit()


class FileOpSignals(QObject):
    """Signals for file operation workers."""

    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(list)


class FileOpWorker(QRunnable):
    """Perform copy/move/delete operations in a separate thread."""

    def __init__(self, op: str, paths: list[str], dest: str | None = None):
        super().__init__()
        self.op = op
        self.paths = paths
        self.dest = dest
        self.signals = FileOpSignals()
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):  # type: ignore[override]
        errors: list[str] = []
        total = len(self.paths)
        for i, path in enumerate(self.paths, 1):
            if self._cancel:
                break
            try:
                if self.op == "copy":
                    base = os.path.basename(path)
                    target = os.path.join(self.dest, base)  # type: ignore[arg-type]
                    if os.path.isdir(path):
                        shutil.copytree(path, target)
                    else:
                        shutil.copy2(path, target)
                elif self.op == "move":
                    base = os.path.basename(path)
                    target = os.path.join(self.dest, base)  # type: ignore[arg-type]
                    shutil.move(path, target)
                elif self.op == "delete":
                    if os.path.isdir(path):
                        shutil.rmtree(path)
                    else:
                        os.remove(path)
            except OSError as e:
                errors.append(f"{path}: {e}")
            self.signals.progress.emit(i, total, path)
        self.signals.finished.emit(errors)


class SearchSignals(QObject):
    """Signals for deep search worker."""

    found = pyqtSignal(str)
    finished = pyqtSignal()


class DeepSearchWorker(QRunnable):
    """Recursively search for files matching a pattern."""

    def __init__(self, root: str, pattern: str):
        super().__init__()
        self.root = root
        self.pattern = pattern
        self.signals = SearchSignals()

    def run(self):  # type: ignore[override]
        def scan(path: str):
            try:
                with os.scandir(path) as it:
                    for entry in it:
                        if fnmatch.fnmatch(entry.name, self.pattern):
                            self.signals.found.emit(entry.path)
                        if entry.is_dir(follow_symlinks=False):
                            scan(entry.path)
            except OSError:
                pass

        scan(self.root)
        self.signals.finished.emit()


def run_file_operation(parent: QWidget, op: str, paths: list[str], dest: str | None = None, on_done=None):
    """Run a file operation with progress dialog and threading."""

    progress = QProgressDialog(f"{op.title()}ing files...", "Cancel", 0, len(paths), parent)
    progress.setWindowModality(Qt.WindowModal)
    progress.setMinimumDuration(0)

    worker = FileOpWorker(op, paths, dest)
    worker.signals.progress.connect(lambda i, t, p: (progress.setValue(i), progress.setLabelText(p)))

    def finish(errors: list[str]):
        progress.close()
        if errors:
            QMessageBox.warning(parent, "Errors", "\n".join(errors))
        if on_done:
            on_done(errors)

    worker.signals.finished.connect(finish)
    progress.canceled.connect(worker.cancel)
    QThreadPool.globalInstance().start(worker)


class EditProxyModel(QSortFilterProxyModel):
    """Proxy model that only allows editing of the first column."""

    def flags(self, index):  # type: ignore[override]
        flags = super().flags(index)
        if index.column() != 0:
            flags &= ~Qt.ItemIsEditable
        return flags


class FileTreeView(QTreeView):
    """Tree view with custom drag-and-drop semantics."""

    def __init__(self, tab: "FileTab"):
        super().__init__(tab)
        self.tab = tab

    def dragEnterEvent(self, event):  # type: ignore[override]
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event):  # type: ignore[override]
        if not event.mimeData().hasUrls():
            return super().dropEvent(event)

        paths = [u.toLocalFile() for u in event.mimeData().urls()]

        idx = self.indexAt(event.pos())
        if idx.isValid():
            src_idx = self.tab.proxy.mapToSource(idx)
            dest = self.tab.model.filePath(src_idx)
            if not os.path.isdir(dest):
                dest = os.path.dirname(dest)
        else:
            dest = self.tab.path

        modifiers = event.keyboardModifiers()
        action = "move" if modifiers & Qt.ShiftModifier else "copy"

        if action == "move":
            dest_dev = os.stat(dest).st_dev
            same_dev = all(os.stat(p).st_dev == dest_dev for p in paths)
            op = "move" if same_dev else "copy"
        else:
            op = "copy"

        new_paths = [os.path.join(dest, os.path.basename(p)) for p in paths]

        def select_new(_errors):
            self.tab.refresh()
            sel = self.tab.view.selectionModel()
            sel.clearSelection()
            for p in new_paths:
                idx = self.tab.model.index(p)
                if idx.isValid():
                    proxy_idx = self.tab.proxy.mapFromSource(idx)
                    sel.select(proxy_idx, QItemSelectionModel.Select | QItemSelectionModel.Rows)

        run_file_operation(self.tab, op, paths, dest, select_new)
        event.acceptProposedAction()


class FileTab(QWidget):
    """A single tab containing a file system view and search box."""

    def __init__(self, path: str):
        super().__init__()
        self.path = path

        layout = QVBoxLayout(self)

        self.breadcrumb_layout = QHBoxLayout()
        layout.addLayout(self.breadcrumb_layout)

        search_layout = QHBoxLayout()

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search...")
        search_layout.addWidget(self.search)

        self.deep_search = QCheckBox("Deep Search")
        search_layout.addWidget(self.deep_search)

        layout.addLayout(search_layout)

        self.results = QListWidget()
        self.results.setMaximumHeight(100)
        self.results.hide()
        self.results.itemDoubleClicked.connect(self.open_result)
        layout.addWidget(self.results)

        self.model = QFileSystemModel()
        self.model.setRootPath(self.path)
        self.model.setReadOnly(False)

        self.proxy = EditProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.proxy.setFilterKeyColumn(0)

        self.view = FileTreeView(self)
        self.view.setModel(self.proxy)
        self.view.setRootIndex(self.proxy.mapFromSource(self.model.index(self.path)))
        self.view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.view.setDragEnabled(True)
        self.view.setAcceptDrops(True)
        self.view.setDragDropMode(QAbstractItemView.DragDrop)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self.open_menu)
        self.view.setEditTriggers(
            QAbstractItemView.EditKeyPressed | QAbstractItemView.SelectedClicked
        )

        f2 = QAction(self)
        f2.setShortcut("F2")
        f2.triggered.connect(lambda: self.view.edit(self.view.currentIndex()))
        self.addAction(f2)

        go_act = QAction(self)
        go_act.setShortcut("Ctrl+L")
        go_act.triggered.connect(self.prompt_path)
        self.addAction(go_act)

        layout.addWidget(self.view)

        self.search.textChanged.connect(self.on_search_text_changed)
        self.deep_search.stateChanged.connect(lambda _s: self.on_search_text_changed(self.search.text()))

        self.update_breadcrumb()

        # directory watcher with debounced refresh
        self.watcher = DirectoryWatcher(self.path)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setSingleShot(True)
        self.refresh_timer.setInterval(300)
        self.refresh_timer.timeout.connect(self.refresh)
        self.watcher.changed.connect(lambda: self.refresh_timer.start())
        self.watcher.start()

        # apply current font
        self.set_font(self.font())

    def refresh(self):
        root = self.model.rootPath()
        self.model.setRootPath("")
        self.model.setRootPath(root)
        self.view.setRootIndex(self.proxy.mapFromSource(self.model.index(root)))

    def cleanup(self):
        self.watcher.stop()

    def set_font(self, font: QFont):
        self.view.setFont(font)
        self.search.setFont(font)

    def cd(self, path: str):
        self.path = path
        self.model.setRootPath(self.path)
        self.view.setRootIndex(self.proxy.mapFromSource(self.model.index(self.path)))
        self.update_breadcrumb()
        parent = self.parentWidget()
        if isinstance(parent, QTabWidget):
            parent.setTabText(parent.indexOf(self), self.path)

    def update_breadcrumb(self):
        while self.breadcrumb_layout.count():
            item = self.breadcrumb_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        parts = Path(self.path).parts
        for i, part in enumerate(parts):
            btn = QToolButton()
            btn.setText(part)
            crumb_path = str(Path(*parts[: i + 1]))
            btn.clicked.connect(lambda _c, p=crumb_path: self.cd(p))
            self.breadcrumb_layout.addWidget(btn)

    def prompt_path(self):
        path, ok = QInputDialog.getText(self, "Go to Path", "Path:", text=self.path)
        if ok and path and os.path.isdir(path):
            self.cd(path)

    def on_search_text_changed(self, text: str):
        self.proxy.setFilterWildcard(text)
        self.results.clear()
        if self.deep_search.isChecked() and text:
            self.results.show()
            worker = DeepSearchWorker(self.path, f"*{text}*")
            worker.signals.found.connect(lambda p: self.results.addItem(p))
            QThreadPool.globalInstance().start(worker)
        else:
            self.results.hide()

    def open_result(self, item):
        path = item.text()
        if os.path.isdir(path):
            self.cd(path)
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    # Context menu implementation
    def open_menu(self, position):
        indexes = self.view.selectedIndexes()
        paths = []
        for idx in indexes:
            if idx.column() == 0:
                source_idx = self.proxy.mapToSource(idx)
                paths.append(self.model.filePath(source_idx))

        menu = QMenu()

        open_act = menu.addAction("Open")
        open_with_act = menu.addAction("Open With...")
        rename_act = menu.addAction("Rename")
        delete_act = menu.addAction("Delete")
        new_folder_act = menu.addAction("New Folder")
        copy_move_act = menu.addAction("Copy/Move")
        prop_act = menu.addAction("Properties")

        if any(path.endswith('.zip') for path in paths):
            extract_act = menu.addAction("Extract Here")
        else:
            extract_act = None

        action = menu.exec_(self.view.viewport().mapToGlobal(position))

        if action == open_act:
            for path in paths:
                QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        elif action == open_with_act and paths:
            self.open_with(paths[0])
        elif action == rename_act and paths:
            self.rename_item(paths[0])
        elif action == delete_act and paths:
            self.delete_items(paths)
        elif action == new_folder_act:
            self.new_folder()
        elif action == copy_move_act and paths:
            self.copy_move(paths)
        elif action == prop_act and paths:
            self.show_properties(paths[0])
        elif action == extract_act and paths:
            self.extract_zip(paths[0])

    # context actions
    def open_with(self, path):
        cmd, ok = QInputDialog.getText(self, "Open With", "Command:", text="xdg-open")
        if ok and cmd:
            try:
                subprocess.Popen([cmd, path])
            except OSError as e:
                QMessageBox.warning(self, "Error", str(e))

    def rename_item(self, path):
        idx = self.model.index(path)
        if idx.isValid():
            proxy_idx = self.proxy.mapFromSource(idx)
            self.view.setCurrentIndex(proxy_idx)
            self.view.edit(proxy_idx)

    def delete_items(self, paths):
        reply = QMessageBox.question(
            self,
            "Delete",
            f"Delete {len(paths)} item(s)?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            run_file_operation(self, "delete", paths)

    def new_folder(self):
        name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
        if ok and name:
            new_dir = os.path.join(self.path, name)
            try:
                os.makedirs(new_dir)
            except OSError as e:
                QMessageBox.warning(self, "Error", str(e))

    def copy_move(self, paths):
        dest = QFileDialog.getExistingDirectory(self, "Select destination")
        if not dest:
            return
        op, ok = QInputDialog.getItem(
            self, "Operation", "Copy or Move?", ["Copy", "Move"], 0, False
        )
        if not ok:
            return
        run_file_operation(self, op.lower(), paths, dest)

    def show_properties(self, path):
        info = Path(path)
        size = info.stat().st_size
        msg = f"Path: {path}\nSize: {size} bytes"
        QMessageBox.information(self, "Properties", msg)

    def extract_zip(self, path):
        try:
            with zipfile.ZipFile(path, 'r') as zf:
                zf.extractall(os.path.dirname(path))
        except (zipfile.BadZipFile, OSError) as e:
            QMessageBox.warning(self, "Error", str(e))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Spleen")
        self.setMinimumSize(600, 400)

        self.settings = QSettings("Spleen", "Spleen")
        self.default_path = self.settings.value("default_path", str(Path.home()))
        self.zoom_factor = float(self.settings.value("zoom", 1.0))
        self.base_font_size = self.font().pointSizeF()

        geometry = self.settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        else:
            self.resize(800, 600)

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        self.clipboard = []
        self.cut_mode = False

        self.create_menus()
        self.new_tab(self.default_path)
        self.apply_zoom()

        self.drives = set()
        self.timer = QTimer()
        self.timer.timeout.connect(self.check_drives)
        self.timer.start(5000)
        self.check_drives()

    def create_menus(self):
        file_menu = self.menuBar().addMenu("File")
        new_tab_act = QAction("New Tab", self)
        new_tab_act.triggered.connect(self.new_tab)
        file_menu.addAction(new_tab_act)
        exit_act = QAction("Exit", self)
        exit_act.triggered.connect(self.close)
        file_menu.addAction(exit_act)

        edit_menu = self.menuBar().addMenu("Edit")
        cut_act = QAction("Cut", self)
        cut_act.triggered.connect(self.cut)
        copy_act = QAction("Copy", self)
        copy_act.triggered.connect(self.copy)
        paste_act = QAction("Paste", self)
        paste_act.triggered.connect(self.paste)
        edit_menu.addActions([cut_act, copy_act, paste_act])

        view_menu = self.menuBar().addMenu("View")
        zoom_in_act = QAction("Zoom In", self)
        zoom_in_act.setShortcut("Ctrl++")
        zoom_in_act.triggered.connect(self.zoom_in)
        zoom_out_act = QAction("Zoom Out", self)
        zoom_out_act.setShortcut("Ctrl+-")
        zoom_out_act.triggered.connect(self.zoom_out)
        reset_zoom_act = QAction("Reset Zoom", self)
        reset_zoom_act.setShortcut("Ctrl+0")
        reset_zoom_act.triggered.connect(self.reset_zoom)
        view_menu.addActions([zoom_in_act, zoom_out_act, reset_zoom_act])

        settings_menu = self.menuBar().addMenu("Settings")
        set_def_act = QAction("Set Default Path", self)
        set_def_act.triggered.connect(self.set_default_path)
        clr_def_act = QAction("Clear Default Path", self)
        clr_def_act.triggered.connect(self.clear_default_path)
        settings_menu.addActions([set_def_act, clr_def_act])

        help_menu = self.menuBar().addMenu("Help")
        about_act = QAction("About", self)
        about_act.triggered.connect(self.show_about)
        help_menu.addAction(about_act)

    # menu actions
    def new_tab(self, path=None):
        if path is None:
            path = QFileDialog.getExistingDirectory(self, "Open Directory", self.default_path)
            if not path:
                return
        tab = FileTab(path)
        tab.set_font(self.font())
        index = self.tabs.addTab(tab, path)
        self.tabs.setCurrentIndex(index)

    def current_tab(self) -> FileTab:
        return self.tabs.currentWidget()  # type: ignore[return-value]

    def cut(self):
        tab = self.current_tab()
        paths = tab.view.selectedIndexes()
        self.clipboard = []
        for idx in paths:
            if idx.column() == 0:
                self.clipboard.append(tab.model.filePath(tab.proxy.mapToSource(idx)))
        self.cut_mode = True

    def copy(self):
        tab = self.current_tab()
        paths = tab.view.selectedIndexes()
        self.clipboard = []
        for idx in paths:
            if idx.column() == 0:
                self.clipboard.append(tab.model.filePath(tab.proxy.mapToSource(idx)))
        self.cut_mode = False

    def paste(self):
        tab = self.current_tab()
        dest = tab.path
        if not self.clipboard:
            return
        paths = list(self.clipboard)
        op = "move" if self.cut_mode else "copy"

        def done(_errors):
            if self.cut_mode:
                self.clipboard = []
                self.cut_mode = False

        run_file_operation(self, op, paths, dest, done)

    def check_drives(self):
        volumes = {
            info.rootPath()
            for info in QStorageInfo.mountedVolumes()
            if info.isValid() and info.isReady()
        }
        new_drives = volumes - self.drives
        for drive in new_drives:
            self.new_tab(drive)
        self.drives = volumes

    def closeEvent(self, event):
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("zoom", self.zoom_factor)
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if isinstance(tab, FileTab):
                tab.cleanup()
        super().closeEvent(event)

    # settings and zoom helpers
    def set_default_path(self):
        path = QFileDialog.getExistingDirectory(self, "Select Default Directory", self.default_path)
        if path:
            self.default_path = path
            self.settings.setValue("default_path", path)

    def clear_default_path(self):
        self.default_path = str(Path.home())
        self.settings.remove("default_path")

    def zoom_in(self):
        self.zoom_factor *= 1.1
        self.apply_zoom()

    def zoom_out(self):
        self.zoom_factor /= 1.1
        self.apply_zoom()

    def reset_zoom(self):
        self.zoom_factor = 1.0
        self.apply_zoom()

    def apply_zoom(self):
        font = QFont()
        font.setPointSizeF(self.base_font_size * self.zoom_factor)
        self.setFont(font)
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if isinstance(tab, FileTab):
                tab.set_font(font)

    def show_about(self):
        QMessageBox.about(
            self,
            "About Spleen",
            "Spleen is a lightweight file manager with tabbed browsing, search, "
            "context menu actions, clipboard operations, drive detection, "
            "configurable default path and a zoomable interface.",
        )


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(
        """
        QWidget { background-color: black; color: #00ff00; }
        QTreeView { selection-background-color: #003300; }
        QMenu { background-color: black; color: #00ff00; }
        QLineEdit { background-color: black; color: #00ff00; }
        """
    )
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
