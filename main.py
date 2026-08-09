"""Windows 透明悬浮控件：实时显示 CPU / 内存占用。"""

from __future__ import annotations

import math
import sys
import winreg
from pathlib import Path

import psutil
from PyQt6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
    QWheelEvent,
)
from PyQt6.QtWidgets import QApplication, QMenu, QWidget

APP_DIR = Path(__file__).resolve().parent
AUTOSTART_NAME = "CpuMemOverlay"
AUTOSTART_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_command() -> str:
    """开机用 wscript 调 run.vbs，避免弹出命令行窗口。"""
    vbs = APP_DIR / "run.vbs"
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
        self.opacity_level = 0.88
        self._drag_offset: QPoint | None = None
        self._resizing = False
        self._compact = False
        self._scale = 1.0

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
            "拖拽移动 · 右下角拖拽缩放 · 滚轮缩放 · 双击紧凑模式 · 右键菜单"
        )
        self._apply_size()
        self._place_near_top_right()

        # 预热 CPU 采样，避免首帧为 0
        psutil.cpu_percent(interval=None)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(self.UPDATE_MS)
        self._refresh()

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

    def _place_near_top_right(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self.move(geo.right() - self.width() - 24, geo.top() + 24)

    def _resize_hit(self, pos: QPoint) -> bool:
        m = self.RESIZE_MARGIN
        return pos.x() >= self.width() - m and pos.y() >= self.height() - m

    def _refresh(self) -> None:
        self.cpu = float(psutil.cpu_percent(interval=None))
        vm = psutil.virtual_memory()
        self.mem = float(vm.percent)
        self.mem_used_gb = vm.used / (1024**3)
        self.mem_total_gb = vm.total / (1024**3)
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
            self._resizing = False
            self._drag_offset = None
            if self._resize_hit(event.position().toPoint()):
                self.setCursor(Qt.CursorShape.SizeFDiagCursor)
            else:
                self.setCursor(Qt.CursorShape.OpenHandCursor)
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

    def _show_menu(self, pos: QPoint) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(
            """
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
        )

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

        autostart = menu.addAction("开机启动")
        autostart.setCheckable(True)
        autostart.setChecked(is_autostart_enabled())
        autostart.triggered.connect(self._toggle_autostart)

        menu.addSeparator()
        quit_action = menu.addAction("退出")
        quit_action.triggered.connect(QApplication.instance().quit)

        menu.exec(pos)

    def _set_opacity(self, value: float) -> None:
        self.opacity_level = value
        self.setWindowOpacity(value)

    def _toggle_compact(self) -> None:
        self._compact = not self._compact
        self._apply_size()
        self.update()

    def _toggle_autostart(self, checked: bool) -> None:
        set_autostart(checked)

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

        # 玻璃底
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)

        fill = QLinearGradient(0, 0, 0, self.height())
        fill.setColorAt(0.0, QColor(22, 30, 40, 200))
        fill.setColorAt(1.0, QColor(12, 16, 22, 220))
        painter.fillPath(path, fill)

        # 顶部微光
        gloss = QLinearGradient(0, 0, 0, self.height() * 0.45)
        gloss.setColorAt(0.0, QColor(255, 255, 255, 28))
        gloss.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.fillPath(path, gloss)

        # 边框
        border = QPen(QColor(255, 255, 255, 42))
        border.setWidthF(max(1.0, 1.2 * s))
        painter.setPen(border)
        painter.drawPath(path)

        # 内描边
        inset = max(1.0, 1.2 * s)
        inner = QRectF(rect.adjusted(inset, inset, -inset, -inset))
        painter.setPen(QPen(QColor(90, 200, 180, 28), max(1.0, 1.0 * s)))
        painter.drawRoundedRect(inner, max(1.0, radius - inset), max(1.0, radius - inset))

        # 内容按基准坐标绘制后整体缩放
        painter.save()
        painter.scale(s, s)
        if self._compact:
            self._paint_compact(painter)
        else:
            self._paint_full(painter)
        painter.restore()

        # 右下角缩放提示
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

        # 外圈柔光（用 float 构造，避免 QPoint 重载在部分环境下崩溃）
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

        # 中心文字
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

        # 端点小圆点
        if percent > 0.5:
            angle = math.radians(90 - 360 * percent / 100.0)
            x = cx + radius * math.cos(angle)
            y = cy - radius * math.sin(angle)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, 220))
            painter.drawEllipse(QPointF(x, y), 2.2, 2.2)


def main() -> int:
    # 高 DPI
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    app.setApplicationName("CPU Mem Overlay")

    window = FloatingMonitor()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
