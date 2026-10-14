"""COM Notifier: иконка в системном трее и уведомления Windows о подключении/отключении COM-портов.

Запуск без окна консоли:  pythonw com_notifier.py
"""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass

from PyQt5.QtCore import QLockFile, QPoint, QPointF, QRect, QSettings, QStandardPaths, Qt, QTimer
from PyQt5.QtGui import QColor, QCursor, QFont, QIcon, QPainter, QPixmap, QPolygonF
from PyQt5.QtWidgets import (
    QAction, QActionGroup, QApplication, QFrame, QGridLayout, QLabel, QMenu, QSystemTrayIcon, QVBoxLayout, QWidget,
)
from serial.tools import list_ports

APP_NAME = "COM Notifier"
ICON_CONNECTED = "🟢"
ICON_DISCONNECTED = "🔴"
POLL_INTERVALS_MS = (50, 200, 1000)
DEFAULT_POLL_INTERVAL_MS = 1000
TOAST_TIMEOUTS_MS = (200, 400, 600, 800, 1500, 2000, 2500, 3000)
DEFAULT_TOAST_TIMEOUT_MS = 800
TOAST_WIDTH = 360
TOAST_MARGIN = 12      # отступ от края экрана и между уведомлениями
REPEAT_WINDOW_SEC = 5
PAUSE_SEC = 30 * 60
POPUP_WIDTH = 440

COLOR_ACTIVE = QColor("#2e7d32")
COLOR_PAUSED = QColor("#9e9e9e")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
SETTINGS_KEY = r"HKEY_CURRENT_USER\Software\ComNotifier"


# ---------------------------------------------------------------- логика портов

@dataclass(frozen=True)
class PortInfo:
    device: str             # "COM3"
    name: str               # имя как в диспетчере устройств, например "USB-SERIAL CH340 (COM3)"
    vid: int | None
    pid: int | None


def scan_ports() -> dict[str, PortInfo]:
    """Текущие COM-порты системы, ключ — имя порта."""
    ports = {}
    for p in list_ports.comports():
        # На Windows description — это FriendlyName устройства из диспетчера устройств.
        ports[p.device] = PortInfo(p.device, p.description or p.device, p.vid, p.pid)
    return ports


def port_sort_key(port: PortInfo):
    m = re.search(r"\d+", port.device)
    return (int(m.group()) if m else 0, port.device)


def sorted_ports(ports: dict[str, PortInfo]) -> list[PortInfo]:
    return sorted(ports.values(), key=port_sort_key)


def diff_ports(old: dict[str, PortInfo], new: dict[str, PortInfo]) -> tuple[list[PortInfo], list[PortInfo]]:
    """Возвращает (подключённые, отключённые) порты, отсортированные по номеру."""
    added = [new[d] for d in new.keys() - old.keys()]
    removed = [old[d] for d in old.keys() - new.keys()]
    return sorted(added, key=port_sort_key), sorted(removed, key=port_sort_key)


def format_vid_pid(port: PortInfo) -> str:
    """'b0de:c0de' или пустая строка, если VID/PID неизвестны."""
    if port.vid is None or port.pid is None:
        return ""
    return f"{port.vid:04x}:{port.pid:04x}"


def short_name(port: PortInfo) -> str:
    """Имя без хвоста ' (COM3)', который дублирует номер порта в списке."""
    return re.sub(rf"\s*\({re.escape(port.device)}\)\s*$", "", port.name) or port.name


def notification_body(port: PortInfo) -> str:
    """Текст уведомления: имя из диспетчера устройств и строка VID:PID."""
    return f"{port.name}\nVID:PID {format_vid_pid(port) or 'н/д'}"


def state_icon(connected: bool) -> str:
    return ICON_CONNECTED if connected else ICON_DISCONNECTED


def format_notification(port: PortInfo, connected: bool) -> tuple[str, str]:
    """Возвращает (заголовок в одну строку, текст из двух строк: имя и VID/PID)."""
    verb = "Подключен" if connected else "Отключен"
    return f"{state_icon(connected)} {verb} {port.device}", notification_body(port)


class RepeatFilter:
    """Защита от «мигания» порта: событие порта пропускается, только если от этого порта
    не было никаких событий последние window секунд. Каждое событие, даже скрытое, перезапускает отсчёт."""

    def __init__(self, window: float = REPEAT_WINDOW_SEC, clock=time.monotonic):
        self.window = window
        self.clock = clock
        self._last: dict[str, float] = {}

    def allow(self, device: str) -> bool:
        now = self.clock()
        last = self._last.get(device)
        self._last[device] = now
        return last is None or now - last >= self.window


class Pause:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.until: float | None = None

    def start(self, seconds: float = PAUSE_SEC) -> None:
        self.until = self.clock() + seconds

    def stop(self) -> None:
        self.until = None

    def remaining(self) -> float:
        """Сколько секунд паузы осталось; 0, если пауза не активна (истёкшая сбрасывается)."""
        if self.until is None:
            return 0
        left = self.until - self.clock()
        if left <= 0:
            self.until = None
            return 0
        return left

    @property
    def active(self) -> bool:
        return self.remaining() > 0


def format_remaining(seconds: float) -> str:
    seconds = int(seconds + 0.999)
    return f"{seconds // 60}:{seconds % 60:02d}"


# ---------------------------------------------------------------- автозапуск (реестр Windows)

def autostart_command() -> str:
    if getattr(sys, "frozen", False):          # собрано в exe
        return f'"{sys.executable}"'
    exe = sys.executable
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pythonw):
        exe = pythonw
    return f'"{exe}" "{os.path.abspath(__file__)}"'


def autostart_enabled() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
        return True
    except OSError:
        return False
    except ImportError:
        return False


def set_autostart(enabled: bool) -> None:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------- интерфейс

def make_icon(color: QColor) -> QIcon:
    """Иконка трея: цветной квадрат с силуэтом разъёма DB-9."""
    size = 256
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(size / 64, size / 64)               # рисуем в координатах 64x64
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    p.drawRoundedRect(QRect(0, 0, 64, 64), 14, 14)
    p.setBrush(Qt.white)
    p.drawPolygon(QPolygonF([QPointF(8, 18), QPointF(56, 18), QPointF(48, 46), QPointF(16, 46)]))
    p.setBrush(color)
    for x in (17, 25, 32, 39, 47):
        p.drawEllipse(QPoint(x, 26), 3, 3)
    for x in (21, 28, 36, 43):
        p.drawEllipse(QPoint(x, 37), 3, 3)
    p.end()
    return QIcon(pm)


class ElidedLabel(QLabel):
    """Метка, обрезающая текст многоточием по доступной ширине."""

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        hint.setWidth(10)
        return hint

    def paintEvent(self, event):
        p = QPainter(self)
        p.setPen(self.palette().color(self.foregroundRole()))
        p.setFont(self.font())
        text = self.fontMetrics().elidedText(self.text(), Qt.ElideRight, self.width())
        p.drawText(self.rect(), int(self.alignment()), text)


# Цвет кружка для emoji состояния: Qt5 на Windows рисует emoji одноцветными,
# поэтому в своих уведомлениях кружок рисуем сами нужным цветом.
EMOJI_COLORS = {
    ICON_CONNECTED: QColor("#3fb950"),
    ICON_DISCONNECTED: QColor("#f85149"),
}


def split_status_emoji(title: str) -> tuple[QColor | None, str]:
    for emoji, color in EMOJI_COLORS.items():
        if title.startswith(emoji):
            return color, title[len(emoji):].lstrip()
    return None, title


class StatusDot(QWidget):
    def __init__(self, color: QColor):
        super().__init__()
        self.color = color
        self.setFixedSize(14, 14)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(self.color)
        p.drawEllipse(self.rect().adjusted(1, 1, -1, -1))


class Toast(QFrame):
    """Одно всплывающее уведомление в стиле Windows 10. Закрывается по таймеру или по клику."""

    def __init__(self, title: str, body: str, timeout_ms: int, on_closed):
        super().__init__(None, Qt.ToolTip | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setObjectName("toast")
        self.setStyleSheet("#toast { background: #1f1f1f; border: 1px solid #3a3a3a; }"
                           "QLabel { color: #ffffff; } QLabel#body { color: #c8c8c8; }")
        self.setFixedWidth(TOAST_WIDTH)
        self._on_closed = on_closed

        color, text = split_status_emoji(title)
        head = QGridLayout()
        head.setHorizontalSpacing(8)
        head.setColumnStretch(1, 1)
        if color is not None:
            head.addWidget(StatusDot(color), 0, 0)
        title_label = ElidedLabel(text)
        title_font = QFont(self.font())
        title_font.setBold(True)
        title_label.setFont(title_font)
        title_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        title_label.setMinimumHeight(title_label.fontMetrics().height())
        head.addWidget(title_label, 0, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(4)
        layout.addLayout(head)
        for line in body.splitlines():
            label = ElidedLabel(line)
            label.setObjectName("body")
            label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            label.setMinimumHeight(label.fontMetrics().height())
            layout.addWidget(label)
        self.adjustSize()

        QTimer.singleShot(timeout_ms, self.close)

    def mousePressEvent(self, event):
        self.close()

    def closeEvent(self, event):
        super().closeEvent(event)
        self._on_closed(self)


class ToastStack:
    """Показывает уведомления стопкой в правом нижнем углу: новые снизу, старые сдвигаются вверх."""

    def __init__(self):
        self.timeout_ms = DEFAULT_TOAST_TIMEOUT_MS
        self.toasts: list[Toast] = []

    def show(self, title: str, body: str) -> None:
        toast = Toast(title, body, self.timeout_ms, self._closed)
        self.toasts.append(toast)
        self._layout()
        toast.show()

    def _closed(self, toast: Toast) -> None:
        if toast in self.toasts:
            self.toasts.remove(toast)
            self._layout()

    def _layout(self) -> None:
        area = QApplication.primaryScreen().availableGeometry()
        y = area.bottom() - TOAST_MARGIN
        for toast in reversed(self.toasts):             # самое новое — внизу
            y -= toast.height()
            if y < area.top():                          # не влезает — закрываем самые старые
                toast.hide()
                continue
            toast.move(area.right() - TOAST_MARGIN - toast.width(), y)
            y -= TOAST_MARGIN


class PortListPopup(QFrame):
    """Всплывающий список COM-портов у иконки трея; закрывается кликом мимо."""

    def __init__(self):
        super().__init__(None, Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setFrameShape(QFrame.StyledPanel)
        self.setObjectName("popup")
        self.setStyleSheet("#popup { border: 1px solid palette(mid); background: palette(window); }")
        self.setFixedWidth(POPUP_WIDTH)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(12, 10, 12, 10)
        self._layout.setSpacing(6)

    def set_ports(self, ports: list[PortInfo]) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                QWidget().setLayout(item.layout())   # удаляет старую сетку

        title = QLabel("COM-порты")
        title.setStyleSheet("color: palette(dark);")
        self._layout.addWidget(title)

        if not ports:
            self._layout.addWidget(QLabel("COM-порты не найдены"))
            self.adjustSize()
            return

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)
        grid.setColumnStretch(1, 1)
        small = QFont(self.font())
        small.setPointSizeF(self.font().pointSizeF() * 0.8)
        bold = QFont(self.font())
        bold.setBold(True)
        for row, port in enumerate(ports):
            num = QLabel(port.device)
            num.setFont(bold)
            name = ElidedLabel(short_name(port))
            name.setToolTip(port.name)
            name.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            vid_pid = QLabel(format_vid_pid(port))
            vid_pid.setFont(small)
            vid_pid.setStyleSheet("color: palette(dark);")
            vid_pid.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            grid.addWidget(num, row, 0)
            grid.addWidget(name, row, 1)
            grid.addWidget(vid_pid, row, 2)
        self._layout.addLayout(grid)
        self.adjustSize()

    def show_near(self, anchor: QRect) -> None:
        """Показать у иконки трея (anchor — её геометрия), не выходя за край экрана."""
        self.adjustSize()
        if not anchor.isValid() or anchor.isEmpty():
            anchor = QRect(QCursor.pos(), QCursor.pos())
        screen = QApplication.screenAt(anchor.center()) or QApplication.primaryScreen()
        area = screen.availableGeometry()
        x = anchor.right() - self.width()
        y = anchor.top() - self.height() - 4          # панель задач снизу
        if y < area.top():                             # панель задач сверху
            y = anchor.bottom() + 4
        x = max(area.left(), min(x, area.right() - self.width()))
        y = max(area.top(), min(y, area.bottom() - self.height()))
        self.move(x, y)
        self.show()
        self.activateWindow()


class TrayApp:
    def __init__(self, app: QApplication):
        self.app = app
        if sys.platform == "win32":
            # Настройки хранятся в реестре, рядом с программой файлов не появляется.
            self.settings = QSettings(SETTINGS_KEY, QSettings.NativeFormat)
        else:
            self.settings = QSettings("ComNotifier", "ComNotifier")
        self.repeat_filter = RepeatFilter()
        self.pause = Pause()
        self.ports = scan_ports()

        self.icon_active = make_icon(COLOR_ACTIVE)
        self.icon_paused = make_icon(COLOR_PAUSED)
        self.tray = QSystemTrayIcon(self.icon_active)
        self.popup = PortListPopup()
        self.pause_timer = QTimer(interval=1000, timeout=self.refresh_pause_ui)

        self.menu = QMenu()
        self.act_autostart = QAction("Автозапуск при старте системы", self.menu, checkable=True)
        self.act_autostart.setChecked(autostart_enabled())
        self.act_autostart.setEnabled(sys.platform == "win32")
        self.act_autostart.toggled.connect(self.on_autostart)

        self.act_no_repeat = QAction(f"Не повторять в течение {REPEAT_WINDOW_SEC} сек", self.menu, checkable=True)
        self.act_no_repeat.setChecked(self.settings.value("no_repeat", True, type=bool))
        self.act_no_repeat.toggled.connect(lambda on: self.settings.setValue("no_repeat", on))


        self.act_pause = QAction(self.menu, checkable=True)
        self.act_pause.toggled.connect(self.on_pause)

        self.toasts = ToastStack()
        self.interval_menu, interval = self._choice_menu(
            "Интервал проверки", "poll_interval_ms", POLL_INTERVALS_MS, DEFAULT_POLL_INTERVAL_MS,
            self.set_poll_interval)
        self.timeout_menu, self.toasts.timeout_ms = self._choice_menu(
            "Время показа уведомлений", "toast_timeout_ms", TOAST_TIMEOUTS_MS, DEFAULT_TOAST_TIMEOUT_MS,
            self.set_toast_timeout)

        act_quit = QAction("Выход", self.menu)
        act_quit.triggered.connect(self.app.quit)

        for a in (self.act_autostart, self.act_no_repeat, self.act_pause):
            self.menu.addAction(a)
        self.menu.addMenu(self.interval_menu)
        self.menu.addMenu(self.timeout_menu)
        self.menu.addSeparator()
        self.menu.addAction(act_quit)
        self.menu.aboutToShow.connect(self.refresh_pause_ui)

        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self.on_activated)
        self.refresh_pause_ui()
        self.tray.show()

        self.poll_timer = QTimer(interval=interval, timeout=self.poll)
        self.poll_timer.start()

    def _setting_action(self, text: str, key: str, default: bool = True) -> QAction:
        act = QAction(text, self.menu, checkable=True)
        act.setChecked(self.settings.value(key, default, type=bool))
        act.toggled.connect(lambda on: self.settings.setValue(key, on))
        return act

    def _choice_menu(self, title: str, key: str, options, default: int, on_change) -> tuple[QMenu, int]:
        """Подменю с выбором одного значения в мс; возвращает (меню, сохранённое значение)."""
        menu = QMenu(title, self.menu)
        group = QActionGroup(menu)
        value = self.settings.value(key, default, type=int)
        if value not in options:
            value = default
        for ms in options:
            act = QAction(f"{ms} мс", group, checkable=True)
            act.setChecked(ms == value)
            act.triggered.connect(lambda _=False, ms=ms: on_change(ms))
            menu.addAction(act)
        return menu, value

    def set_toast_timeout(self, ms: int) -> None:
        self.toasts.timeout_ms = ms
        self.settings.setValue("toast_timeout_ms", ms)

    def set_poll_interval(self, ms: int) -> None:
        self.poll_timer.setInterval(ms)
        self.settings.setValue("poll_interval_ms", ms)

    # --- опрос портов и уведомления

    def poll(self) -> None:
        try:
            current = scan_ports()
        except Exception:
            return
        added, removed = diff_ports(self.ports, current)
        self.ports = current
        for port, connected in [(p, False) for p in removed] + [(p, True) for p in added]:
            self.on_port_event(port, connected)
        if (added or removed) and self.popup.isVisible():
            self.popup.set_ports(sorted_ports(self.ports))

    def on_port_event(self, port: PortInfo, connected: bool) -> None:
        allowed = self.repeat_filter.allow(port.device)   # учитываем всегда, даже на паузе
        if allowed or not self.act_no_repeat.isChecked():
            self.show(*format_notification(port, connected))

    def show(self, title: str, body: str) -> None:
        if self.pause.active:
            return
        self.toasts.show(title, body)

    # --- трей

    def on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.Trigger:           # левый клик
            if self.popup.isVisible():
                self.popup.hide()
                return
            self.ports = scan_ports()
            self.popup.set_ports(sorted_ports(self.ports))
            self.popup.show_near(self.tray.geometry())

    def on_autostart(self, enabled: bool) -> None:
        try:
            set_autostart(enabled)
        except Exception as e:
            self.toasts.show(APP_NAME, f"Не удалось изменить автозапуск: {e}")
            self.act_autostart.blockSignals(True)
            self.act_autostart.setChecked(autostart_enabled())
            self.act_autostart.blockSignals(False)

    def on_pause(self, enabled: bool) -> None:
        if enabled:
            self.pause.start()
            self.pause_timer.start()
        else:
            self.pause.stop()
            self.pause_timer.stop()
        self.refresh_pause_ui()

    def refresh_pause_ui(self) -> None:
        left = self.pause.remaining()
        paused = left > 0
        if not paused:
            self.pause_timer.stop()
        self.act_pause.blockSignals(True)
        self.act_pause.setChecked(paused)
        self.act_pause.blockSignals(False)
        if paused:
            self.act_pause.setText(f"Пауза на 30 мин (осталось {format_remaining(left)})")
            self.tray.setIcon(self.icon_paused)
            self.tray.setToolTip(f"{APP_NAME}: пауза, осталось {format_remaining(left)}")
        else:
            self.act_pause.setText("Пауза на 30 мин")
            self.tray.setIcon(self.icon_active)
            self.tray.setToolTip(APP_NAME)


def main() -> int:
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)

    # Не запускать вторую копию (например, при ручном запуске поверх автозапуска).
    lock = QLockFile(os.path.join(QStandardPaths.writableLocation(QStandardPaths.TempLocation), "com_notifier.lock"))
    if not lock.tryLock(100):
        return 0

    if not QSystemTrayIcon.isSystemTrayAvailable():
        return 1
    tray_app = TrayApp(app)  # noqa: F841  держим ссылку, пока работает цикл событий
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
