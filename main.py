"""Windows 透明悬浮控件：实时显示 CPU / 内存占用。"""

from __future__ import annotations

import json
import math
import sys
import winreg
from pathlib import Path

import psutil
from PyQt6.QtCore import (
    QLockFile,
    QPoint,
    QPointF,
    QRectF,
    Qt,
    QThread,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
    QWheelEvent,
)
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget

APP_NAME = "CPU Mem Overlay"
AUTOSTART_NAME = "CpuMemOverlay"
AUTOSTART_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
SINGLETON_KEY = "CpuMemOverlaySingleton"
EMA_ALPHA = 0.35


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def settings_path() -> Path:
    base = Path.home() / "AppData" / "Roaming" / "CpuMemOverlay"
    base.mkdir(parents=True, exist_ok=True)
    return base / "settings.json"


def lock_path() -> Path:
    base = Path.home() / "AppData" / "Local" / "CpuMemOverlay"
    base.mkdir(parents=True, exist_ok=True)
    return base / "instance.lock"


def load_settings() -> dict:
    path = settings_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(data: dict) -> None:
    path = settings_path()
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def autostart_command() -> str:
    """优先启动打包后的 exe；开发模式走 run.vbs（会自动选 exe/pythonw）。"""
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'
    for candidate in (app_dir() / "CpuMemOverlay.exe", app_dir() / "dist" / "CpuMemOverlay.exe"):
        if candidate.exists():
            return f'"{candidate}"'
    vbs = app_dir() / "run.vbs"
    return f'wscript.exe "{vbs}"'


def is_autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_REG_PATH) as key:
            value, _ = winreg.QueryValueEx(key, AUTOSTART_NAME)
            return bool(value)
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        AUTOSTART_REG_PATH,
        0,
        winreg.KEY_SET_VALUE,
    ) as key:
        if enabled:
            winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(key, AUTOSTART_NAME)
            except FileNotFoundError:
                pass


def make_tray_icon() -> QIcon:
    size = 64
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setBrush(QColor(22, 30, 40, 240))
    painter.setPen(QPen(QColor(90, 200, 180, 220), 3))
    painter.drawRoundedRect(4, 4, size - 8, size - 8, 14, 14)
    pen = QPen(QColor(90, 200, 180), 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawArc(14, 14, size - 28, size - 28, 40 * 16, 280 * 16)
    painter.end()
    return QIcon(pm)


def ema(prev: float | None, value: float, alpha: float = EMA_ALPHA) -> float:
    if prev is None:
        return value
    return prev * (1.0 - alpha) + value * alpha


class Sampler(QThread):
    """后台采样，避免阻塞 UI 绘制。"""

    sample_ready = pyqtSignal(float, float, float, float)

    def __init__(self, interval_ms: int = 1000, parent=None) -> None:
        super().__init__(parent)
        self._interval_ms = max(200, interval_ms)

    def run(self) -> None:
        psutil.cpu_percent(interval=None)
        while not self.isInterruptionRequested():
            cpu = float(psutil.cpu_percent(interval=None))
            vm = psutil.virtual_memory()
            self.sample_ready.emit(
                cpu,
                float(vm.percent),
                vm.used / (1024**3),
                vm.total / (1024**3),
            )
            self.msleep(self._interval_ms)


class FloatingMonitor(QWidget):
    BASE_W = 220
    BASE_H = 128
    COMPACT_W = 168
    COMPACT_H = 56
    MIN_SCALE = 0.65
    MAX_SCALE = 2.8
    RESIZE_MARGIN = 16
    UPDATE_MS = 1000

    def __init__(self) -> None:
        super().__init__()
        self.cpu = 0.0
        self.mem = 0.0
        self.mem_used_gb = 0.0
        self.mem_total_gb = 0.0
        self._cpu_raw: float | None = None
        self._mem_raw: float | None = None
        self.opacity_level = 0.88
        self._drag_offset: QPoint | None = None
        self._resizing = False
        self._compact = False
        self._scale = 1.0
        self._settings_timer = QTimer(self)
        self._settings_timer.setSingleShot(True)
        self._settings_timer.timeout.connect(self._persist_settings)

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)
        self.setWindowOpacity(self.opacity_level)
        self.setToolTip(
            "拖拽移动 · 右下角拖拽缩放 · 滚轮缩放 · 双击紧凑模式 · 右键菜单 · 托盘退出"
        )

        self._restore_settings()
        self._apply_size()
        if not self._has_saved_pos:
            self._place_near_top_right()

        self._sampler = Sampler(self.UPDATE_MS, self)
        self._sampler.sample_ready.connect(self._on_sample)
        self._sampler.start()

        self._tray: QSystemTrayIcon | None = None
        self._setup_tray()

    def _restore_settings(self) -> None:
        data = load_settings()
        self._has_saved_pos = "x" in data and "y" in data
        self._scale = self._clamp_scale(float(data.get("scale", 1.0)))
        self._compact = bool(data.get("compact", False))
        self.opacity_level = float(data.get("opacity", 0.88))
        self.opacity_level = max(0.35, min(1.0, self.opacity_level))
        self.setWindowOpacity(self.opacity_level)
        if self._has_saved_pos:
            self.move(int(data["x"]), int(data["y"]))

    def _schedule_persist(self) -> None:
        self._settings_timer.start(250)

    def _persist_settings(self) -> None:
        save_settings(
            {
                "x": self.x(),
                "y": self.y(),
                "scale": round(self._scale, 3),
                "compact": self._compact,
                "opacity": round(self.opacity_level, 3),
            }
        )

    def _setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        tray = QSystemTrayIcon(make_tray_icon(), self)
        menu = QMenu()
        menu.setStyleSheet(self._menu_style())
        show_action = menu.addAction("显示 / 隐藏")
        show_action.triggered.connect(self.toggle_visibility)
        menu.addSeparator()
        quit_action = menu.addAction("退出")
        quit_action.triggered.connect(self._quit_app)
        tray.setContextMenu(menu)
        tray.setToolTip(APP_NAME)
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        self._tray = tray

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.toggle_visibility()

    def toggle_visibility(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.bring_to_front()

    def bring_to_front(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _quit_app(self) -> None:
        self._persist_settings()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _base_size(self) -> tuple[int, int]:
        if self._compact:
            return self.COMPACT_W, self.COMPACT_H
        return self.BASE_W, self.BASE_H

    def _apply_size(self) -> None:
        bw, bh = self._base_size()
        w = max(1, int(round(bw * self._scale)))
        h = max(1, int(round(bh * self._scale)))
        self.setFixedSize(w, h)

    def _clamp_scale(self, scale: float) -> float:
        return max(self.MIN_SCALE, min(self.MAX_SCALE, scale))

    def _set_scale(self, scale: float) -> None:
        new_scale = self._clamp_scale(scale)
        if abs(new_scale - self._scale) < 0.001:
            return
        self._scale = new_scale
        self._apply_size()
        self.update()
        self._schedule_persist()

    def _place_near_top_right(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self.move(geo.right() - self.width() - 24, geo.top() + 24)

    def _resize_hit(self, pos: QPoint) -> bool:
        m = self.RESIZE_MARGIN
        return pos.x() >= self.width() - m and pos.y() >= self.height() - m

    def _on_sample(self, cpu: float, mem: float, used_gb: float, total_gb: float) -> None:
        self._cpu_raw = ema(self._cpu_raw, cpu)
        self._mem_raw = ema(self._mem_raw, mem)
        self.cpu = self._cpu_raw
        self.mem = self._mem_raw
        self.mem_used_gb = used_gb
        self.mem_total_gb = total_gb
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            if self._resize_hit(event.position().toPoint()):
                self._resizing = True
                self.setCursor(Qt.CursorShape.SizeFDiagCursor)
            else:
                self._drag_offset = (
                    event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                )
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
        elif event.button() == Qt.MouseButton.RightButton:
            self._show_menu(event.globalPosition().toPoint())
            event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position().toPoint()

        if self._resizing and event.buttons() & Qt.MouseButton.LeftButton:
            top_left = self.frameGeometry().topLeft()
            global_pos = event.globalPosition().toPoint()
            new_w = max(1, global_pos.x() - top_left.x())
            new_h = max(1, global_pos.y() - top_left.y())
            bw, bh = self._base_size()
            scale = max(new_w / bw, new_h / bh)
            self._set_scale(scale)
            event.accept()
            return

        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return

        if self._resize_hit(pos):
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        elif event.buttons() == Qt.MouseButton.NoButton:
            self.setCursor(Qt.CursorShape.OpenHandCursor)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            moved_or_resized = self._resizing or self._drag_offset is not None
            self._resizing = False
            self._drag_offset = None
            if self._resize_hit(event.position().toPoint()):
                self.setCursor(Qt.CursorShape.SizeFDiagCursor)
            else:
                self.setCursor(Qt.CursorShape.OpenHandCursor)
            if moved_or_resized:
                self._schedule_persist()
            event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and not self._resize_hit(
            event.position().toPoint()
        ):
            self._toggle_compact()
            event.accept()

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        delta = event.angleDelta().y()
        if delta == 0:
            return
        step = 0.08 if delta > 0 else -0.08
        self._set_scale(self._scale + step)
        event.accept()

    @staticmethod
    def _menu_style() -> str:
        return """
            QMenu {
                background: rgba(18, 24, 32, 230);
                color: #e8eef4;
                border: 1px solid rgba(255,255,255,40);
                border-radius: 8px;
                padding: 6px;
            }
            QMenu::item {
                padding: 6px 24px;
                border-radius: 5px;
            }
            QMenu::item:selected {
                background: rgba(90, 200, 180, 55);
            }
        """

    def _show_menu(self, pos: QPoint) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(self._menu_style())

        opacity_menu = menu.addMenu("透明度")
        for label, value in (
            ("高亮 100%", 1.0),
            ("清晰 88%", 0.88),
            ("半透 70%", 0.70),
            ("轻透 50%", 0.50),
        ):
            action = opacity_menu.addAction(label)
            action.triggered.connect(lambda _=False, v=value: self._set_opacity(v))

        size_menu = menu.addMenu("大小")
        for label, scale in (
            ("较小 80%", 0.8),
            ("默认 100%", 1.0),
            ("较大 130%", 1.3),
            ("很大 180%", 1.8),
        ):
            action = size_menu.addAction(label)
            action.triggered.connect(lambda _=False, s=scale: self._set_scale(s))

        toggle = menu.addAction("紧凑模式" if not self._compact else "完整模式")
        toggle.triggered.connect(self._toggle_compact)

        hide_action = menu.addAction("隐藏到托盘")
        hide_action.triggered.connect(self.hide)

        autostart = menu.addAction("开机启动")
        autostart.setCheckable(True)
        autostart.setChecked(is_autostart_enabled())
        autostart.triggered.connect(self._toggle_autostart)

        menu.addSeparator()
        quit_action = menu.addAction("退出")
        quit_action.triggered.connect(self._quit_app)

        menu.exec(pos)

    def _set_opacity(self, value: float) -> None:
        self.opacity_level = value
        self.setWindowOpacity(value)
        self._schedule_persist()

    def _toggle_compact(self) -> None:
        self._compact = not self._compact
        self._apply_size()
        self.update()
        self._schedule_persist()

    def _toggle_autostart(self, checked: bool) -> None:
        set_autostart(checked)

    def shutdown(self) -> None:
        self._persist_settings()
        if self._sampler.isRunning():
            self._sampler.requestInterruption()
            self._sampler.wait(1500)

    @staticmethod
    def _usage_color(percent: float, cool: QColor, hot: QColor) -> QColor:
        t = max(0.0, min(1.0, (percent - 40.0) / 55.0))
        return QColor(
            int(cool.red() + (hot.red() - cool.red()) * t),
            int(cool.green() + (hot.green() - cool.green()) * t),
            int(cool.blue() + (hot.blue() - cool.blue()) * t),
            255,
        )

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        s = self._scale
        radius = (18.0 if not self._compact else 14.0) * s

        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)

        fill = QLinearGradient(0, 0, 0, self.height())
        fill.setColorAt(0.0, QColor(22, 30, 40, 200))
        fill.setColorAt(1.0, QColor(12, 16, 22, 220))
        painter.fillPath(path, fill)

        gloss = QLinearGradient(0, 0, 0, self.height() * 0.45)
        gloss.setColorAt(0.0, QColor(255, 255, 255, 28))
        gloss.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.fillPath(path, gloss)

        border = QPen(QColor(255, 255, 255, 42))
        border.setWidthF(max(1.0, 1.2 * s))
        painter.setPen(border)
        painter.drawPath(path)

        inset = max(1.0, 1.2 * s)
        inner = QRectF(rect.adjusted(inset, inset, -inset, -inset))
        painter.setPen(QPen(QColor(90, 200, 180, 28), max(1.0, 1.0 * s)))
        painter.drawRoundedRect(inner, max(1.0, radius - inset), max(1.0, radius - inset))

        painter.save()
        painter.scale(s, s)
        if self._compact:
            self._paint_compact(painter)
        else:
            self._paint_full(painter)
        painter.restore()

        self._paint_resize_grip(painter)

    def _paint_resize_grip(self, painter: QPainter) -> None:
        x = self.width() - 11
        y = self.height() - 11
        painter.setPen(Qt.PenStyle.NoPen)
        for i, alpha in enumerate((55, 90, 130)):
            painter.setBrush(QColor(210, 230, 235, alpha))
            painter.drawEllipse(QPointF(x + i * 3.2, y + i * 3.2), 1.5, 1.5)

    def _paint_compact(self, painter: QPainter) -> None:
        cpu_color = self._usage_color(self.cpu, QColor(90, 200, 180), QColor(255, 120, 90))
        mem_color = self._usage_color(self.mem, QColor(120, 180, 255), QColor(255, 160, 70))

        font = QFont("Segoe UI", 10, QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor(230, 238, 244, 235))
        painter.drawText(
            14, 22, 70, 20, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "CPU"
        )
        painter.drawText(
            14,
            34,
            140,
            20,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            f"{self.cpu:4.1f}%",
        )

        painter.drawText(
            92, 22, 70, 20, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "MEM"
        )
        painter.drawText(
            92,
            34,
            140,
            20,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            f"{self.mem:4.1f}%",
        )

        self._draw_bar(painter, QRectF(14, 44, 68, 5), self.cpu, cpu_color)
        self._draw_bar(painter, QRectF(92, 44, 62, 5), self.mem, mem_color)

    def _paint_full(self, painter: QPainter) -> None:
        title_font = QFont("Segoe UI", 8, QFont.Weight.Medium)
        painter.setFont(title_font)
        painter.setPen(QColor(170, 190, 205, 180))
        painter.drawText(
            16, 10, 180, 16, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "SYSTEM"
        )

        cpu_color = self._usage_color(self.cpu, QColor(90, 200, 180), QColor(255, 120, 90))
        mem_color = self._usage_color(self.mem, QColor(120, 180, 255), QColor(255, 160, 70))

        self._draw_ring(painter, QPoint(62, 72), 34, self.cpu, cpu_color, "CPU", f"{self.cpu:.0f}%")
        self._draw_ring(painter, QPoint(158, 72), 34, self.mem, mem_color, "MEM", f"{self.mem:.0f}%")

        sub = QFont("Segoe UI", 7)
        painter.setFont(sub)
        painter.setPen(QColor(150, 170, 185, 160))
        painter.drawText(
            0,
            108,
            self.BASE_W,
            14,
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            f"{self.mem_used_gb:.1f} / {self.mem_total_gb:.1f} GB",
        )

    def _draw_bar(self, painter: QPainter, rect: QRectF, percent: float, color: QColor) -> None:
        track = QPainterPath()
        track.addRoundedRect(rect, 3, 3)
        painter.fillPath(track, QColor(255, 255, 255, 22))

        width = max(3.0, rect.width() * max(0.0, min(100.0, percent)) / 100.0)
        fill_rect = QRectF(rect.x(), rect.y(), width, rect.height())
        fill = QPainterPath()
        fill.addRoundedRect(fill_rect, 3, 3)
        painter.fillPath(fill, color)

    def _draw_ring(
        self,
        painter: QPainter,
        center: QPoint,
        radius: float,
        percent: float,
        color: QColor,
        label: str,
        value: str,
    ) -> None:
        cx, cy = float(center.x()), float(center.y())

        glow = QRadialGradient(cx, cy, radius + 8)
        glow_color = QColor(color)
        glow_color.setAlpha(40)
        glow.setColorAt(0.55, QColor(0, 0, 0, 0))
        glow.setColorAt(0.75, glow_color)
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(QPointF(cx, cy), radius + 8, radius + 8)

        track_pen = QPen(
            QColor(255, 255, 255, 24), 7.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap
        )
        painter.setPen(track_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        ring = QRectF(cx - radius, cy - radius, radius * 2, radius * 2)
        painter.drawArc(ring, 0, 360 * 16)

        span = int(-360 * 16 * max(0.0, min(100.0, percent)) / 100.0)
        active = QPen(color, 7.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        painter.setPen(active)
        painter.drawArc(ring, 90 * 16, span)

        label_font = QFont("Segoe UI", 7, QFont.Weight.Medium)
        painter.setFont(label_font)
        painter.setPen(QColor(160, 180, 195, 190))
        painter.drawText(
            QRectF(cx - 28, cy - 18, 56, 14),
            Qt.AlignmentFlag.AlignCenter,
            label,
        )

        value_font = QFont("Segoe UI", 12, QFont.Weight.DemiBold)
        painter.setFont(value_font)
        painter.setPen(QColor(240, 246, 250, 245))
        painter.drawText(
            QRectF(cx - 30, cy - 2, 60, 22),
            Qt.AlignmentFlag.AlignCenter,
            value,
        )

        if percent > 0.5:
            angle = math.radians(90 - 360 * percent / 100.0)
            x = cx + radius * math.cos(angle)
            y = cy - radius * math.sin(angle)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, 220))
            painter.drawEllipse(QPointF(x, y), 2.2, 2.2)


def _notify_existing_instance() -> bool:
    socket = QLocalSocket()
    socket.connectToServer(SINGLETON_KEY)
    if not socket.waitForConnected(300):
        return False
    socket.write(b"show\n")
    socket.flush()
    socket.waitForBytesWritten(300)
    socket.disconnectFromServer()
    return True


def _install_singleton_server(window: FloatingMonitor) -> QLocalServer:
    QLocalServer.removeServer(SINGLETON_KEY)
    server = QLocalServer(window)

    def on_new_connection() -> None:
        client = server.nextPendingConnection()
        if client is None:
            return

        def on_ready() -> None:
            _ = client.readAll()
            window.bring_to_front()
            client.disconnectFromServer()

        client.readyRead.connect(on_ready)

    server.newConnection.connect(on_new_connection)
    server.listen(SINGLETON_KEY)
    return server


def main() -> int:
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("CpuMemOverlay")

    lock = QLockFile(str(lock_path()))
    lock.setStaleLockTime(10_000)
    if not lock.tryLock(100):
        if _notify_existing_instance():
            return 0
        # 锁残留但进程已死时清理后重试
        lock.removeStaleLockFile()
        if not lock.tryLock(100):
            if _notify_existing_instance():
                return 0
            return 1

    window = FloatingMonitor()
    server = _install_singleton_server(window)
    # 防止局部变量被回收导致锁提前释放
    app._instance_lock = lock  # type: ignore[attr-defined]
    app._instance_server = server  # type: ignore[attr-defined]
    window.show()

    def on_about_to_quit() -> None:
        window.shutdown()
        server.close()
        QLocalServer.removeServer(SINGLETON_KEY)
        lock.unlock()

    app.aboutToQuit.connect(on_about_to_quit)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
