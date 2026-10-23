"""Заставка «Универсальный скачиватель запускается…» у часов.

Ярлык запускает программу без окна, а до открытия страницы проходит несколько секунд (импорт yt-dlp,
ожидание старой вкладки, запуск браузера) — без заставки непонятно, запустилось ли что-то.
Только stdlib (ctypes): показывается первым делом, до тяжёлых импортов app.py. Окно живёт в своём потоке;
не получилось (не Windows, ошибка) — show() отдаёт заглушку, программа запускается как обычно.
"""

import ctypes
import threading
import time
from ctypes import wintypes

TITLE = 'Универсальный скачиватель'
WIDTH, HEIGHT = 340, 84    # в точках при 100 % масштаба
LIFETIME = 60              # на всякий случай: дольше заставка не висит, даже если её забыли закрыть
FADE = 0.15                # появление и исчезание, секунд

THEMES = {  # цвета страницы (index.html): фон карточки, рамка, текст, подпись, полоса, дорожка полосы
    'light': (0xffffff, 0xe1e4ea, 0x15171c, 0x5f6673, 0x2f62ea, 0xeaf0ff),
    'dark': (0x181b21, 0x2a2f38, 0xeceef2, 0xa0a7b3, 0x4a6cf0, 0x1d2643),
}

WM_DESTROY, WM_PAINT, WM_TIMER, WM_LBUTTONUP, WM_ERASEBKGND = 0x0002, 0x000F, 0x0113, 0x0202, 0x0014
WM_APP_TEXT, WM_APP_CLOSE = 0x8001, 0x8002
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [('style', wintypes.UINT), ('lpfnWndProc', WNDPROC), ('cbClsExtra', ctypes.c_int),
                ('cbWndExtra', ctypes.c_int), ('hInstance', wintypes.HINSTANCE), ('hIcon', wintypes.HICON),
                ('hCursor', wintypes.HANDLE), ('hbrBackground', wintypes.HBRUSH),
                ('lpszMenuName', wintypes.LPCWSTR), ('lpszClassName', wintypes.LPCWSTR)]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [('hdc', wintypes.HDC), ('fErase', wintypes.BOOL), ('rcPaint', wintypes.RECT),
                ('fRestore', wintypes.BOOL), ('fIncUpdate', wintypes.BOOL), ('rgbReserved', ctypes.c_byte * 32)]


def _dlls():
    """Свои экземпляры user32/gdi32 с типами для 64-битных указателей (общий windll не трогаем)."""
    u, g = ctypes.WinDLL('user32'), ctypes.WinDLL('gdi32')
    H, P = wintypes.HANDLE, ctypes.c_void_p
    u.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_int, H, H, H, P]
    u.CreateWindowExW.restype = wintypes.HWND
    u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.DefWindowProcW.restype = LRESULT
    for name in ('DestroyWindow', 'UpdateWindow'):
        getattr(u, name).argtypes = [wintypes.HWND]
    u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.SetTimer.argtypes = [wintypes.HWND, P, wintypes.UINT, P]
    u.SetTimer.restype = P
    u.KillTimer.argtypes = [wintypes.HWND, P]
    u.InvalidateRect.argtypes = [wintypes.HWND, P, wintypes.BOOL]
    u.BeginPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
    u.BeginPaint.restype = wintypes.HDC
    u.EndPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
    u.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF, wintypes.BYTE, wintypes.DWORD]
    u.SetWindowRgn.argtypes = [wintypes.HWND, H, wintypes.BOOL]
    u.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, P, wintypes.UINT]
    u.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    u.TranslateMessage.argtypes = u.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    u.LoadImageW.argtypes = [H, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    u.LoadImageW.restype = H
    u.LoadCursorW.argtypes = [H, P]
    u.LoadCursorW.restype = H
    u.DrawIconEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, H, ctypes.c_int, ctypes.c_int,
                             wintypes.UINT, H, wintypes.UINT]
    u.DrawTextW.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.RECT), wintypes.UINT]
    u.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), H]
    u.DestroyIcon.argtypes = [H]
    g.CreateCompatibleDC.argtypes = [wintypes.HDC]
    g.CreateCompatibleDC.restype = wintypes.HDC
    g.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    g.CreateCompatibleBitmap.restype = H
    g.SelectObject.argtypes = [wintypes.HDC, H]
    g.SelectObject.restype = H
    g.DeleteObject.argtypes = [H]
    g.DeleteDC.argtypes = [wintypes.HDC]
    g.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HDC,
                         ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    g.CreateSolidBrush.argtypes = [wintypes.COLORREF]
    g.CreateSolidBrush.restype = H
    g.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.COLORREF]
    g.CreatePen.restype = H
    g.RoundRect.argtypes = [wintypes.HDC] + [ctypes.c_int] * 6
    g.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
    g.CreateRoundRectRgn.restype = H
    g.CreateFontW.argtypes = [ctypes.c_int] * 5 + [wintypes.DWORD] * 8 + [wintypes.LPCWSTR]
    g.CreateFontW.restype = H
    g.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
    g.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
    return u, g


def bgr(rgb):
    """0xRRGGBB → COLORREF (0x00BBGGRR)."""
    return ((rgb & 0xff) << 16) | (rgb & 0xff00) | (rgb >> 16)


def dark_theme():
    """Тёмная тема приложений Windows (Параметры → Цвета)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize') as k:
            return winreg.QueryValueEx(k, 'AppsUseLightTheme')[0] == 0
    except OSError:
        return False


class Splash:
    def __init__(self, text, icon):
        self.text = text
        self.icon_path = icon
        self.hwnd = None
        self.closing = None       # время начала исчезания
        self.close_at = None      # закрыть в это время (close(delay))
        self.started = time.monotonic()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, name='splash', daemon=True)
        self.thread.start()
        self.ready.wait(2)

    # ---- вызывается из любых потоков ----

    def set_text(self, text):
        self.text = text
        if self.hwnd:
            self.u.PostMessageW(self.hwnd, WM_APP_TEXT, 0, 0)

    def close(self, text=None, delay=0.0):
        """Закрыть (с новой надписью — через delay секунд, чтобы её успели прочитать)."""
        if text:
            self.set_text(text)
        self.close_at = time.monotonic() + delay
        if self.hwnd:
            self.u.PostMessageW(self.hwnd, WM_APP_CLOSE, 0, 0)

    def wait(self, timeout=5):
        """Дождаться, пока заставка исчезнет (перед выходом из процесса)."""
        self.thread.join(timeout)

    @property
    def alive(self):
        return self.hwnd is not None and self.closing is None and self.close_at is None

    # ---- поток окна ----

    def _run(self):
        try:
            self._create()
        except Exception:  # noqa: BLE001 — без заставки программа всё равно запустится
            self.hwnd = None
            self.ready.set()
            return
        self.ready.set()
        msg = wintypes.MSG()
        while self.u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            self.u.TranslateMessage(ctypes.byref(msg))
            self.u.DispatchMessageW(ctypes.byref(msg))
        self.hwnd = None

    def _create(self):
        self.u, self.g = u, g = _dlls()
        try:  # чёткий текст при масштабе 125–200 %: DPI-режим только для этого потока, остальную программу не трогаем
            u.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
            u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
            self.scale = u.GetDpiForSystem() / 96
        except (AttributeError, OSError):
            self.scale = 1.0
        s = self.scale
        self.w, self.h = round(WIDTH * s), round(HEIGHT * s)
        self.colors = [bgr(c) for c in THEMES['dark' if dark_theme() else 'light']]
        self.font_title = g.CreateFontW(-round(15 * s), 0, 0, 0, 600, 0, 0, 0, 1, 0, 0, 5, 0, 'Segoe UI')
        self.font_text = g.CreateFontW(-round(13 * s), 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 5, 0, 'Segoe UI')
        self.icon = u.LoadImageW(None, self.icon_path, 1, round(32 * s), round(32 * s), 0x10) if self.icon_path else None

        self._proc = WNDPROC(self._wndproc)  # держим ссылку, иначе сборщик мусора уберёт обработчик
        wc = WNDCLASSW(lpfnWndProc=self._proc, lpszClassName='UdlSplash', hCursor=u.LoadCursorW(None, 32512))
        u.RegisterClassW(ctypes.byref(wc))
        area = wintypes.RECT()  # рабочая область без панели задач: ставим в правый нижний угол, к часам
        u.SystemParametersInfoW(0x0030, 0, ctypes.byref(area), 0)  # SPI_GETWORKAREA
        margin = round(12 * s)
        x, y = area.right - self.w - margin, area.bottom - self.h - margin
        # поверх всех, без кнопки на панели задач, не забирает фокус, полупрозрачность для плавного появления
        ex = 0x00000008 | 0x00000080 | 0x08000000 | 0x00080000
        self.hwnd = u.CreateWindowExW(ex, 'UdlSplash', TITLE, 0x80000000, x, y, self.w, self.h, None, None, None, None)
        if not self.hwnd:
            raise OSError('CreateWindowEx')
        r = round(10 * s)
        u.SetWindowRgn(self.hwnd, g.CreateRoundRectRgn(0, 0, self.w + 1, self.h + 1, r, r), True)
        u.SetLayeredWindowAttributes(self.hwnd, 0, 0, 2)  # LWA_ALPHA
        u.ShowWindow(self.hwnd, 4)  # SW_SHOWNOACTIVATE
        u.SetTimer(self.hwnd, 1, 30, None)
        if self.close_at is not None:
            u.PostMessageW(self.hwnd, WM_APP_CLOSE, 0, 0)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TIMER:
                self._tick()
                return 0
            if msg == WM_PAINT:
                self._paint()
                return 0
            if msg == WM_ERASEBKGND:
                return 1
            if msg == WM_APP_TEXT:
                self.u.InvalidateRect(hwnd, None, False)
                return 0
            if msg == WM_APP_CLOSE:
                return 0  # само закрытие — в _tick, когда подойдёт close_at
            if msg == WM_LBUTTONUP:  # щелчок — убрать заставку
                self.close_at = time.monotonic()
                return 0
            if msg == WM_DESTROY:
                self.u.KillTimer(hwnd, 1)
                for obj in (self.font_title, self.font_text):
                    self.g.DeleteObject(obj)
                if self.icon:
                    self.u.DestroyIcon(self.icon)
                self.u.PostQuitMessage(0)
                return 0
        except Exception:  # noqa: BLE001 — исключение в обработчике окна уронило бы поток
            pass
        return self.u.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _tick(self):
        now = time.monotonic()
        if self.closing is None and (now - self.started > LIFETIME or (self.close_at is not None and now >= self.close_at)):
            self.closing = now
        if self.closing is not None:
            alpha = 1 - (now - self.closing) / FADE
            if alpha <= 0:
                self.u.DestroyWindow(self.hwnd)
                return
        else:
            alpha = min(1.0, (now - self.started) / FADE)
        self.u.SetLayeredWindowAttributes(self.hwnd, 0, round(255 * alpha), 2)
        self.u.InvalidateRect(self.hwnd, None, False)

    def _paint(self):
        u, g, s = self.u, self.g, self.scale
        bg, line, text, muted, accent, track = self.colors
        ps = PAINTSTRUCT()
        hdc = u.BeginPaint(self.hwnd, ctypes.byref(ps))
        mem = g.CreateCompatibleDC(hdc)  # рисуем в памяти и копируем разом — без мерцания
        bmp = g.CreateCompatibleBitmap(hdc, self.w, self.h)
        old_bmp = g.SelectObject(mem, bmp)
        brush, pen = g.CreateSolidBrush(bg), g.CreatePen(0, max(1, round(s)), line)
        old_brush, old_pen = g.SelectObject(mem, brush), g.SelectObject(mem, pen)
        r = round(10 * s)
        g.RoundRect(mem, 0, 0, self.w, self.h, r, r)
        g.SelectObject(mem, old_brush)
        g.SelectObject(mem, old_pen)
        g.DeleteObject(brush)
        g.DeleteObject(pen)

        pad, icon = round(16 * s), round(32 * s)
        if self.icon:
            u.DrawIconEx(mem, pad, (self.h - icon) // 2 - round(4 * s), self.icon, icon, icon, 0, None, 3)
        left = pad + icon + round(14 * s)
        g.SetBkMode(mem, 1)  # TRANSPARENT
        for font, color, top, label in ((self.font_title, text, 17, TITLE), (self.font_text, muted, 39, self.text)):
            old_font = g.SelectObject(mem, font)
            g.SetTextColor(mem, color)
            rect = wintypes.RECT(left, round(top * s), self.w - pad, round((top + 20) * s))
            u.DrawTextW(mem, label, -1, ctypes.byref(rect), 0x0020 | 0x8000)  # одна строка, «…» если не влезла
            g.SelectObject(mem, old_font)

        # полоса «идёт работа»: отрезок бежит слева направо по дорожке
        bar_top, bar_h, bar_w = round(62 * s), max(2, round(3 * s)), self.w - left - pad
        self._fill(mem, left, bar_top, bar_w, bar_h, track)
        phase = ((time.monotonic() - self.started) % 1.4) / 1.4
        seg = bar_w // 3
        x0 = left + round((bar_w + seg) * phase) - seg
        a, b = max(left, x0), min(left + bar_w, x0 + seg)
        if b > a:
            self._fill(mem, a, bar_top, b - a, bar_h, accent)

        g.BitBlt(hdc, 0, 0, self.w, self.h, mem, 0, 0, 0x00CC0020)  # SRCCOPY
        g.SelectObject(mem, old_bmp)
        g.DeleteObject(bmp)
        g.DeleteDC(mem)
        u.EndPaint(self.hwnd, ctypes.byref(ps))

    def _fill(self, hdc, x, y, w, h, color):
        brush = self.g.CreateSolidBrush(color)
        self.u.FillRect(hdc, ctypes.byref(wintypes.RECT(x, y, x + w, y + h)), brush)
        self.g.DeleteObject(brush)


class NoSplash:
    """Заглушка, если окно не создалось."""
    alive = False

    def set_text(self, text):
        pass

    def close(self, text=None, delay=0.0):
        pass

    def wait(self, timeout=5):
        pass


def show(text, icon=None):
    try:
        splash = Splash(text, icon)
    except Exception:  # noqa: BLE001
        return NoSplash()
    return splash if splash.hwnd else NoSplash()
