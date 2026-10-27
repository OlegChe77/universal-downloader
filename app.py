"""Универсальный скачиватель: локальный сервер + страница в браузере.

Работает на yt-dlp (1000+ сайтов: YouTube, VK, Rutube, OK, Дзен, TikTok...)
и ffmpeg (склейка видео со звуком, конвертация в MP3).
"""
import base64
import concurrent.futures
import gc
import importlib.util
import io
import itertools
import json
import math
import logging
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
TRAY = '--tray' in sys.argv
DEFAULT_PORT = 8765


def early_settings():
    """settings.json как есть — до тяжёлых импортов (load_settings ниже)."""
    try:
        saved = json.loads((BASE / 'settings.json').read_text(encoding='utf-8'))
        return saved if isinstance(saved, dict) else {}
    except (OSError, ValueError):
        return {}


def settings_port():
    """Порт страницы: DOWNLOADER_PORT (для тестов) или «port» из settings.json. Нужен до тяжёлых импортов."""
    try:
        port = int(os.environ.get('DOWNLOADER_PORT') or early_settings().get('port') or DEFAULT_PORT)
    except (TypeError, ValueError):
        return DEFAULT_PORT
    return port if 1024 <= port <= 65535 else DEFAULT_PORT


import i18n
from i18n import tr

i18n.set_lang(early_settings().get('lang'))
PORT = settings_port()
ERROR_ALREADY_EXISTS = 183
MUTEX = None  # мьютекс Windows «копия на этом порту работает или запускается» — держим, пока живы


def take_mutex():
    """True — других копий на этом порту нет (мьютекс наш)."""
    global MUTEX, kernel32
    if os.name != 'nt':
        return True
    import ctypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    MUTEX = kernel32.CreateMutexW(None, True, f'Local\\udl-app-{PORT}')
    return not MUTEX or ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def wait_first_copy():
    """Другая копия ещё запускается (на медленном ПК — десятки секунд, а ярлык и кнопку расширения жмут снова —
    у часов оказывалось по 4 копии). Ждём, пока она займёт порт (тогда main() просто покажет её страницу)
    или завершится (тогда запускаемся сами — мьютекс наш). True — мьютекс наш."""
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        if kernel32.WaitForSingleObject(MUTEX, 500) in (0, 0x80):  # WAIT_OBJECT_0, WAIT_ABANDONED
            return True
        with socket.socket() as s:
            if s.connect_ex(('127.0.0.1', PORT)) == 0:
                return False
    return False


def open_log():
    """Окна нет — всё, что программа пишет (и ошибки запуска), сохраняем в файл. Только копия с мьютексом:
    вторая копия раньше затирала журнал работающей."""
    if TRAY:
        sys.stdout = sys.stderr = open(BASE / 'program.log', 'w', encoding='utf-8', buffering=1)


FIRST = True
SPLASH = None
if __name__ == '__main__':
    FIRST = take_mutex()
    if FIRST:
        open_log()
    elif '--no-browser' in sys.argv:  # страницу открывать не нужно, а программа запускается и без нас
        sys.exit()
    # Заставка у часов — до тяжёлых импортов: без окна иначе несколько секунд не видно, запустилось ли что-то
    if TRAY and os.name == 'nt':
        import splash
        splash.TITLE = tr(splash.TITLE, 'Universal Downloader')
        SPLASH = splash.show(tr('Запускается…', 'Starting…') if FIRST
                             else tr('Уже запускается — подождите…', 'Already starting — please wait…'),
                             str(BASE / 'web' / 'icon.ico'))
    if not FIRST:
        FIRST = wait_first_copy()
        if FIRST:
            open_log()

import imageio_ffmpeg
import yt_dlp
import yt_dlp.downloader.external
import yt_dlp.networking.exceptions
import yt_dlp.postprocessor.ffmpeg
from yt_dlp.postprocessor.metadataparser import MetadataParserPP
from yt_dlp.utils import download_range_func
from flask import Flask, Response, jsonify, request, send_file, send_from_directory

import sites
import updater

sites.register()  # свои разборщики: ЯП файлы, ЯПлакалъ

WEB = BASE / 'web'
ICON = WEB / 'icon.ico'
BIN = BASE / 'bin'
DOWNLOADS = BASE / 'Скачанное'
COOKIES = BASE / 'cookies.txt'
SETTINGS = BASE / 'settings.json'
EXTENSION = BASE / 'extension'
VERSION = '1.2.1'  # номер этой версии = тег релиза на GitHub без «v» (updater.py сверяет их)
UPDATE_API = (os.environ.get('DOWNLOADER_UPDATE_API')  # для проверки обновления без настоящего релиза
              or 'https://api.github.com/repos/OlegChe77/universal-downloader/releases/latest')
UPDATE_DIR = BASE / 'update'  # скачанная новая версия и старые файлы на время замены
UPDATE_CACHE = Path(sys.prefix) / 'app-update.json'  # когда проверяли и что нашли
UPDATE_EVERY = 3 * 24 * 3600
DEV_COPY = (BASE / '.git').exists()  # папка разработки обновляется через git, а не архивом
# proxy — прокси VPN-программы; proxy_all — все сайты через него; proxy_sites — сайты, которые не открылись напрямую
# doh — защищённый DNS: адреса, которые не нашёл системный DNS, искать через DNS по HTTPS
DEFAULT_SETTINGS = {'download_dir': str(DOWNLOADS), 'ask_every_time': True, 'port': DEFAULT_PORT,
                    'proxy': '', 'proxy_all': False, 'proxy_sites': [], 'doh': False,
                    'lang': ''}  # язык: ru, en или пусто — как в Windows (i18n.py)


def APP_NAME():
    return tr('Универсальный скачиватель', 'Universal Downloader')
HOST = '127.0.0.1'
PROTOCOL = 'udl-downloader'  # ссылка udl-downloader: запускает программу (так её включает расширение браузера)

PRESET_HEIGHTS = [2160, 1440, 1080, 720, 480, 360]
SEARCH_LIMIT = 10
ARTIST_CUT = r'^(.{40}).+$'  # исполнитель или канал в имени файла — не длиннее 40 букв
TITLE_JUNK = (r'(?i)\s*[(\[][^)\]]*(official|lyric|audio|video|clip|клип|премьера|текст'
              r'|visuali[sz]er|\bhd\b|\b4k\b)[^)\]]*[)\]]')

# deno (JS-движок для YouTube) ставится через pip в папку Scripts (во встроенном Python она не рядом
# с python.exe) — добавляем в PATH, чтобы yt-dlp его нашёл.
# bin с ffmpeg — тоже: проверка «можно ли качать кусок видео» ищет ffmpeg только в PATH, без ffmpeg_location
os.environ['PATH'] = os.pathsep.join([sysconfig.get_path('scripts'), str(Path(sys.executable).parent), str(BIN),
                                      os.environ.get('PATH', '')])

app = Flask(__name__)
tasks = {}
tasks_lock = threading.Lock()
download_queue = queue.Queue()  # загрузки из списка ссылок идут по одной
request_seq = itertools.count(1)
task_of_thread = {}  # поток загрузки → его задача, чтобы при отмене убить ffmpeg


class TrackedPopen(yt_dlp.utils.Popen):
    """Запоминает запущенный ffmpeg в задаче. Фрагменты ffmpeg качает сам, без
    промежуточных отчётов, и без этого «Отмена» ждала бы конца загрузки."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        task = task_of_thread.get(threading.get_ident())
        if task is not None:
            task['procs'].append(self)
            cmd = args[0] if args else kwargs.get('args')
            if isinstance(cmd, list) and cmd:  # последний аргумент ffmpeg — файл, куда он пишет
                task['tmp'].add(str(cmd[-1]).removeprefix('file:'))
            if task['cancel']:
                self.kill()


yt_dlp.downloader.external.Popen = TrackedPopen
yt_dlp.postprocessor.ffmpeg.Popen = TrackedPopen


def prepare_ffmpeg():
    """Кладёт ffmpeg из imageio-ffmpeg в bin/ffmpeg.exe, чтобы yt-dlp его нашёл."""
    BIN.mkdir(exist_ok=True)
    target = BIN / ('ffmpeg.exe' if os.name == 'nt' else 'ffmpeg')
    if not target.exists():
        shutil.copy2(imageio_ffmpeg.get_ffmpeg_exe(), target)
    return BIN


def load_settings():
    try:
        saved = json.loads(SETTINGS.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        saved = {}
    return {**DEFAULT_SETTINGS, **{k: v for k, v in saved.items() if k in DEFAULT_SETTINGS}}


def save_settings(**changes):
    settings = {**load_settings(), **changes}
    SETTINGS.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding='utf-8')
    return settings


LOCAL_HOSTS = {f'127.0.0.1:{PORT}', f'localhost:{PORT}'}
LOCAL_ORIGINS = {f'http://{h}' for h in LOCAL_HOSTS}


@app.before_request
def only_own_page():
    """Сервер слушает только 127.0.0.1, но браузер пускает к нему любую открытую страницу.
    - чужой Host — это DNS rebinding (домен злоумышленника, указывающий на 127.0.0.1): отказ;
    - к /api/* — только со своей страницы: Origin и Sec-Fetch-Site у запросов с чужих сайтов другие;
    - POST — только JSON: такой запрос с чужого сайта браузер не отправит без разрешения (CORS),
      а обычная HTML-форма JSON не шлёт.
    Запросы без Origin/Sec-Fetch-* (curl, тесты, проверка «уже запущено») пропускаем."""
    if request.host.lower() not in LOCAL_HOSTS:
        return Response('Forbidden', 403, mimetype='text/plain')
    if not request.path.startswith('/api/'):
        return None
    origin = request.headers.get('Origin')
    if (origin and origin not in LOCAL_ORIGINS) or request.headers.get('Sec-Fetch-Site') in ('cross-site', 'same-site'):
        return jsonify(error=tr('Запрос с чужой страницы.', 'Request from another page.')), 403
    if request.method == 'POST' and not request.is_json:
        return jsonify(error=tr('Нужен JSON.', 'JSON required.')), 415
    return None


@app.after_request
def safety_headers(resp):
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['X-Frame-Options'] = 'SAMEORIGIN'  # чужой сайт не встроит страницу в рамку (кликджекинг)
    resp.headers['Content-Security-Policy'] = "frame-ancestors 'self'"
    resp.headers['Referrer-Policy'] = 'no-referrer'  # сайтам с обложками незачем знать адрес программы
    if request.path.startswith('/api/'):
        resp.headers['Cross-Origin-Resource-Policy'] = 'same-origin'
    return resp


def json_body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def text_arg(data, key):
    value = data.get(key)
    return value.strip() if isinstance(value, str) else ''


def download_dir():
    """Папка для загрузок из настроек; если её удалили — папка по умолчанию."""
    folder = Path(load_settings()['download_dir'])
    return folder if folder.is_dir() else DOWNLOADS


# Окно выбора папки запускаем отдельным процессом: tkinter не любит работать из потоков Flask
PICK_FOLDER_SCRIPT = r'''
import sys, tkinter
from tkinter import filedialog
root = tkinter.Tk()
root.withdraw()
root.attributes('-topmost', True)
path = filedialog.askdirectory(parent=root, initialdir=sys.argv[1], title=sys.argv[2], mustexist=True)
print(path or '')
'''

# Во встроенном Python нет tkinter — тогда окно выбора папки из Windows через PowerShell.
# Начальная папка — через переменную окружения, чтобы не возиться с кавычками.
# Окно должно оказаться поверх браузера: владелец — невидимая TopMost-форма, её надо показать
# (у не показанной формы TopMost не действует), а фокус Windows отдаёт только активному процессу —
# поэтому на миг подключаемся к потоку окна браузера (AttachThreadInput) и забираем фокус себе.
PICK_FOLDER_PS = r'''
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
Add-Type -Namespace Udl -Name Win -MemberDefinition @"
[DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
[DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, IntPtr pid);
[DllImport("user32.dll")] public static extern bool AttachThreadInput(uint a, uint b, bool attach);
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
[DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
"@
$owner = New-Object System.Windows.Forms.Form -Property @{
    TopMost = $true; ShowInTaskbar = $false; FormBorderStyle = 'None'; Opacity = 0
    StartPosition = 'CenterScreen'; Size = New-Object System.Drawing.Size(1, 1)
}
$owner.Show()
$fg = [Udl.Win]::GetWindowThreadProcessId([Udl.Win]::GetForegroundWindow(), [IntPtr]::Zero)
$me = [Udl.Win]::GetCurrentThreadId()
if ($fg -and $fg -ne $me) { [void][Udl.Win]::AttachThreadInput($me, $fg, $true) }
[void][Udl.Win]::SetForegroundWindow($owner.Handle)
$owner.Activate()
if ($fg -and $fg -ne $me) { [void][Udl.Win]::AttachThreadInput($me, $fg, $false) }
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = $env:UDL_PICK_TITLE
$dialog.SelectedPath = $env:UDL_PICK_INITIAL
if ($dialog.ShowDialog($owner) -eq 'OK') { $dialog.SelectedPath }
'''


def pick_folder(initial):
    env = {**os.environ, 'PYTHONIOENCODING': 'utf-8', 'UDL_PICK_INITIAL': str(initial),
           'UDL_PICK_TITLE': tr('Куда сохранить файлы?', 'Where to save files?')}
    if importlib.util.find_spec('tkinter'):
        cmd = [sys.executable, '-c', PICK_FOLDER_SCRIPT, str(initial), env['UDL_PICK_TITLE']]
    else:
        script = base64.b64encode(PICK_FOLDER_PS.encode('utf-16-le')).decode()
        cmd = ['powershell.exe', '-NoProfile', '-STA', '-EncodedCommand', script]
    out = subprocess.run(cmd, capture_output=True, encoding='utf-8', env=env)
    path = out.stdout.strip()
    return str(Path(path)) if path else None


def _user32():
    """Свой экземпляр user32 с типами для 64-битных HWND (общий windll.user32 не трогаем)."""
    import ctypes
    from ctypes import wintypes
    u = ctypes.WinDLL('user32')
    u.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM]
    for name in ('GetClassNameW', 'GetWindowTextW'):
        getattr(u, name).argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    for name in ('IsWindowVisible', 'IsIconic', 'BringWindowToTop', 'SetForegroundWindow'):
        getattr(u, name).argtypes = [wintypes.HWND]
    u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u.GetForegroundWindow.restype = wintypes.HWND
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, wintypes.LPVOID]
    u.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    u.PeekMessageW.argtypes = [wintypes.LPVOID, wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
    return u


def explorer_windows():
    """Видимые окна Проводника: {hwnd: заголовок}."""
    import ctypes
    from ctypes import wintypes
    u = _user32()
    found = {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        buf = ctypes.create_unicode_buffer(512)
        u.GetClassNameW(hwnd, buf, 512)
        if buf.value == 'CabinetWClass' and u.IsWindowVisible(hwnd):
            u.GetWindowTextW(hwnd, buf, 512)
            found[hwnd] = buf.value
        return True

    u.EnumWindows(each, 0)
    return found


def bring_to_front(hwnd):
    """Как в PICK_FOLDER_PS: Windows отдаёт фокус только активному процессу, поэтому на миг
    подключаемся к потоку активного окна (браузера) и делаем окно Проводника активным."""
    import ctypes
    from ctypes import wintypes
    u = _user32()
    u.PeekMessageW(ctypes.byref(wintypes.MSG()), None, 0, 0, 0)  # без очереди сообщений AttachThreadInput не работает
    if u.IsIconic(hwnd):
        u.ShowWindow(hwnd, 9)  # SW_RESTORE
    fg = u.GetWindowThreadProcessId(u.GetForegroundWindow(), None)
    me = ctypes.windll.kernel32.GetCurrentThreadId()
    attached = bool(fg and fg != me and u.AttachThreadInput(me, fg, True))
    try:
        u.BringWindowToTop(hwnd)
        u.SetForegroundWindow(hwnd)
    finally:
        if attached:
            u.AttachThreadInput(me, fg, False)
    return u.GetForegroundWindow() == hwnd


def show_in_explorer(path, select=False):
    """Открывает папку (или папку с выделенным файлом) в Проводнике поверх всех окон.
    Окно открывает уже запущенный explorer.exe, не наш процесс, а уже открытую папку он только
    подсвечивает на панели задач — окно остаётся за браузером. Ждём новое окно (или открытое
    с этой папкой — его узнаём по заголовку) и выводим вперёд."""
    path = Path(path)
    folder = path.parent if select else path
    before = set(explorer_windows())
    if select:
        subprocess.Popen(f'explorer /select,"{path}"')
    else:
        os.startfile(path)  # уже открытую папку покажет в её окне, а не во втором

    def raise_window(hwnd):
        for _ in range(5):  # только что созданное окно иногда не сразу принимает фокус
            if bring_to_front(hwnd):
                return
            time.sleep(0.2)

    def focus():
        names = {folder.name.lower(), str(folder).lower()}
        start = time.time()
        raised_old = False
        while time.time() - start < 6:
            time.sleep(0.1)
            now = explorer_windows()
            new = [h for h in now if h not in before]
            if new:
                return raise_window(new[0])
            elapsed = time.time() - start
            # новое окно не появилось — значит, Проводник показал папку в открытом окне;
            # выводим его (пока он занят запросом, фокус не даётся — пробуем снова),
            # но ещё немного ждём: с /select он иногда открывает новое
            same = [h for h, title in now.items() if title.lower() in names]
            if same and not raised_old and elapsed > 0.5:
                raised_old = bring_to_front(same[0])
            if raised_old and elapsed > 3:
                return

    threading.Thread(target=focus, daemon=True).start()


def base_opts():
    opts = {
        'quiet': True,
        'no_warnings': True,
        'noprogress': True,
        'noplaylist': True,
        'ffmpeg_location': str(BIN),
        # YouTube требует JS-движок для расшифровки ссылок; подойдёт Deno или Node.js
        'js_runtimes': {'deno': {}, 'node': {}},
    }
    if COOKIES.exists():
        opts['cookiefile'] = str(COOKIES)
    return opts


class Text:
    """Текст на двух языках: str() — на текущем, «+» склеивает оба языка."""
    def __init__(self, ru, en):
        self.ru, self.en = ru, en

    def __add__(self, other):
        return Text(self.ru + other.ru, self.en + other.en)

    def __str__(self):
        return tr(self.ru, self.en)


VPN_HINT = Text(' Если включён VPN — выключите его или смените сервер.',
                ' If a VPN is on, turn it off or switch to another server.')
COOKIES_HINT = Text(' Нужен cookies.txt из браузера, где вы вошли на сайт: положите его в папку программы (как — на странице «Помощь»).',
                    ' You need cookies.txt from a browser where you are signed in to the site: put it in the program folder (see the Help page).')
UPDATE_HINT = Text(' Обновите yt-dlp на странице «Помощь».', ' Update yt-dlp on the Help page.')
BLOCK_HINT = Text(' Если сайт заблокирован (YouTube, Instagram, Pornhub…), нужен VPN для всего компьютера или его прокси: '
                  'VPN-расширение в браузере на программу не действует. Как быть — «Помощь» → «Если включён VPN».',
                  ' If the site is blocked in your country, you need a VPN for the whole computer or its proxy: '
                  'a VPN browser extension does not affect the program. See Help → “If you use a VPN”.')
DOH_HINT = Text(' Интернет есть, а провайдер прячет адрес сайта — включите «Защищённый DNS» на вкладке «VPN».',
                ' If the internet works but your provider hides the site address, turn on “Secure DNS” on the Proxy tab.')

# (регулярное выражение по тексту ошибки, объяснение). Порядок важен: сначала узкие случаи.
# yt-dlp дописывает к разным ошибкам «Use --cookies…», поэтому «вход в аккаунт» стоит после
# «не бот», «18+» и платного; 403/404 — по точным словам, а не по цифрам в адресе.
ERROR_RULES = [
    # yt-dlp сам заблокировал некоторые сайты (Suno и др.) из-за юридических рисков
    (r'\[liability\]|will not be supported',
     Text('Этот сайт yt-dlp не поддерживает намеренно (по юридическим причинам) — обновление не поможет. '
          'Скачайте файл на самом сайте, если он это позволяет.',
          'yt-dlp deliberately does not support this site (for legal reasons) — updating will not help. '
          'Download the file on the site itself if it allows that.')),
    (r'\bdrm\b',
     Text('Видео защищено DRM (как в Netflix, Кинопоиске, Okko, Spotify) — скачать его нельзя.',
          'The video is DRM-protected (like Netflix, Disney+, Spotify) — it cannot be downloaded.')),
    (r"not a bot|confirm you.?re not",
     Text('YouTube просит подтвердить, что вы не робот. Так бывает с VPN и с адресами, откуда качают много.',
          'YouTube asks to confirm you are not a bot. This happens with VPNs and with addresses that download a lot.')
     + VPN_HINT + COOKIES_HINT),
    (r'confirm your age|age[- ]restricted|inappropriate for some users|18\+|age verification|must be 18|'
     r'not be comfortable for some audiences',
     Text('Видео с возрастным ограничением (18+): сайт отдаёт его только после входа в аккаунт.',
          'Age-restricted video (18+): the site only gives it to signed-in users.') + COOKIES_HINT),
    (r'members[- ]only|join this channel|\bpremium\b|requires? (a )?(payment|purchase|subscription)|\bpaid\b|\brental\b',
     Text('Видео платное или только для подписчиков — скачать можно, только если у вас есть доступ.',
          'The video is paid or members-only — you can download it only if you have access.') + COOKIES_HINT),
    (r'premieres? in|live event will begin|is upcoming',
     Text('Трансляция или премьера ещё не началась. Попробуйте, когда видео появится.',
          'The stream or premiere has not started yet. Try again when the video is out.')),
    (r'geo.?restrict|not available (in|from) your (country|location)|in your country|blocked it in your',
     Text('Видео недоступно в вашей стране — сайт ограничил показ по месту.'
          ' С зарубежным VPN так бывает у русских сайтов (Rutube, VK, ok.ru), без VPN — у зарубежных.',
          'The video is not available in your country — the site restricts it by location.'
          ' A proxy in another country may help (the Proxy tab).')),
    (r'\bprivate\b',
     Text('Видео закрыто автором (приватное) — скачать можно, только если у вашего аккаунта есть доступ.',
          'The video is private — you can download it only if your account has access.') + COOKIES_HINT),
    # «This content isn't available, try again later… rate-limited» — это не удалённое видео, а ограничение
    (r'rate.?limit|http error 429|too many requests',
     Text('Сайт временно ограничил загрузки: слишком много запросов. Подождите 10–30 минут.',
          'The site has temporarily limited downloads: too many requests. Wait 10–30 minutes.') + VPN_HINT),
    # TikTok так отвечает (статус 10204) и на удалённый пост, и на закрытый для страны
    (r'ip address is blocked',
     Text('TikTok не отдаёт этот пост: его удалили или он закрыт для вашей страны. Проверьте ссылку в браузере; '
          'если открывается только с VPN — включите прокси во вкладке «VPN».',
          'TikTok does not give this post: it was deleted or is closed for your country. Check the link in a browser; '
          'if it opens only with a VPN, set up the proxy on the Proxy tab.')),
    # Instagram без входа показывает не всё: закрытые аккаунты, 18+, а иногда просто «много запросов»
    (r'empty media response',
     Text('Instagram не показал этот пост без входа в аккаунт: аккаунт закрытый, пост 18+ '
          'или Instagram ограничил просмотр без входа.',
          'Instagram did not show this post without signing in: the account is private, the post is 18+ '
          'or Instagram limited viewing without an account.') + COOKIES_HINT),
    (r'video (is )?(unavailable|not available)|has been removed|no longer available|'
     r'been deleted|does not exist|was deleted',
     Text('Видео удалено или недоступно. Проверьте ссылку в браузере.',
          'The video was deleted or is unavailable. Check the link in a browser.')),
    (r'unsupported url',
     Text('Этот сайт не поддерживается или по ссылке нет видео. Проверьте, что ссылка ведёт на страницу '
          'с самим видео, а не на ленту или главную.',
          'This site is not supported or there is no video at the link. Make sure the link leads to the page '
          'of the video itself, not to a feed or the home page.')),
    (r'no video in this post',
     Text('В этом посте нет видео — только фото. Программа качает видео и звук.',
          'This post has no video — only photos. The program downloads video and audio.')),
    (r'is not a valid url|no such file or directory: .?(<url>|https?)',
     Text('Это не похоже на ссылку. Скопируйте адрес из строки браузера целиком.',
          'This does not look like a link. Copy the whole address from the browser address bar.')),
    (r'sign in|log ?in|cookies|authenticat|registered users',
     Text('Сайт требует вход в аккаунт.', 'The site requires signing in.') + COOKIES_HINT),
    # разборщик сайта в yt-dlp не понял новую страницу (так было у ok.ru — исправлено в sites.py)
    (r'the json object must be|unable to extract|failed to parse|unexpected (response|data)|'
     r'please report this issue',
     Text('Сайт изменил страницу, и yt-dlp не смог её разобрать.',
          'The site has changed its page and yt-dlp could not read it.') + UPDATE_HINT
     + Text(' Не поможет — подождите исправления, обычно его выпускают за несколько дней.',
            ' If that does not help, wait for a fix — it usually comes out within a few days.')),
    (r'requested format is not available|no video formats found',
     Text('Такого качества у видео больше нет. Разберите ссылку заново и выберите другой вариант.',
          'This quality is no longer available. Check the link again and choose another option.')),
    # 3419392776 — так ffmpeg сообщает HTTP 404 (AVERROR_HTTP_NOT_FOUND)
    (r'http error 404|404: not found|code 3419392776',
     Text('Видео удалено или ссылка устарела (ошибка 404). Откройте его в браузере; если играет, '
          'нажмите правой кнопкой на самом видео → «Скачать в программе».',
          'The video was deleted or the link is outdated (error 404). Open it in a browser; if it plays, '
          'right-click the video itself → “Send to Downloader”.')),
    # 410 — страницу убрали насовсем, но так же отвечают сайты, ушедшие из страны (PornHub и др.)
    (r'http error 410|410: gone',
     Text('Страница удалена или сайт закрыт для вашей страны: проверьте ссылку в браузере; '
          'если открывается только с VPN — включите прокси во вкладке «VPN».',
          'The page was deleted or the site is closed for your country: check the link in a browser; '
          'if it opens only with a VPN, set up the proxy on the Proxy tab.')),
    # 3436169992 — так ffmpeg сообщает HTTP 403 (AVERROR_HTTP_FORBIDDEN), бывает при загрузке куска
    (r'http error 403|forbidden|code 3436169992',
     Text('Сайт отказал в доступе (ошибка 403). Попробуйте ещё раз через минуту. Ссылки на видео привязаны '
          'к адресу — VPN нельзя включать или переключать во время загрузки. '
          'Повторяется — обновите yt-dlp на странице «Помощь».',
          'The site denied access (error 403). Try again in a minute. Video links are tied to your IP '
          'address — do not turn a VPN on or switch it during a download. '
          'If it keeps happening, update yt-dlp on the Help page.')),
    (r'http error 5\d\d|service unavailable|bad gateway|internal server error',
     Text('У сайта сбой на его стороне. Попробуйте позже.', 'The site has a problem on its side. Try again later.')),
    # так провайдер обрывает соединение с заблокированным сайтом
    (r'unexpected_eof|eof occurred in violation',
     Text('Сайт не открывается: соединение оборвали — похоже, он заблокирован.',
          'The site does not open: the connection was cut — it looks blocked.') + BLOCK_HINT),
    (r'certificate|\bssl\b|\btls\b',
     Text('Не удалось установить защищённое соединение с сайтом. Проверьте дату и время на компьютере; '
          'так бывает, когда антивирус проверяет HTTPS или VPN подменяет соединение.',
          'Could not make a secure connection to the site. Check the date and time on the computer; '
          'this also happens when an antivirus inspects HTTPS or a VPN intercepts the connection.')),
    (r'getaddrinfo|name or service not known|failed to resolve|nodename nor servname|no address associated',
     Text('Не удалось найти сайт в сети. Проверьте интернет.', 'Could not find the site. Check your internet connection.')
     + DOH_HINT + BLOCK_HINT),
    (r'timed out|timeout|connection (reset|refused|aborted)|remote end closed|network is unreachable|'
     r'unable to connect|max retries|winerror 100(5\d|6\d)|incompleteread|connection broken|got only',
     Text('Сайт не отвечает или связь прервалась. Проверьте интернет и попробуйте ещё раз.',
          'The site does not respond or the connection dropped. Check your internet and try again.') + BLOCK_HINT),
    (r'no space left|errno 28|winerror 112|not enough space',
     Text('На диске не хватает места. Освободите место или выберите другую папку для загрузок.',
          'Not enough disk space. Free up space or choose another download folder.')),
    (r'being used by another process|winerror 32',
     Text('Файл занят другой программой (плеером или антивирусом). Закройте его и попробуйте снова.',
          'The file is in use by another program (a player or antivirus). Close it and try again.')),
    (r'file name too long|errno 36|winerror 206|filename.*too long',
     Text('Слишком длинный путь к файлу. Выберите папку для загрузок поближе к корню диска, например D:\\Загрузки.',
          'The file path is too long. Choose a download folder closer to the drive root, for example D:\\Downloads.')),
    (r'permission denied|errno 13|winerror 5\b|access is denied',
     Text('Нет доступа к папке загрузок. Выберите другую папку или проверьте, не блокирует ли её антивирус.',
          'No access to the download folder. Choose another folder or check that an antivirus is not blocking it.')),
    (r'downloaded file is empty|did not get any data|empty file',
     Text('Сайт отдал пустой файл. Попробуйте ещё раз.', 'The site returned an empty file. Try again.') + VPN_HINT),
    (r'ffmpeg|postprocessing|conversion failed|merg',
     Text('Не удалось склеить или перевести файл (ffmpeg). Проверьте строку «ffmpeg» на странице «Помощь» '
          'или выберите другой вариант.',
          'Could not merge or convert the file (ffmpeg). Check the “ffmpeg” line on the Help page '
          'or choose another option.')),
]
ERROR_RULES = [(re.compile(rx, re.I), text) for rx, text in ERROR_RULES]
ERROR_PREFIX = re.compile(r'^(\[[^\]]+\] [^\s:]+: )+')
URL_IN_TEXT = re.compile(r'\b(https?|file)://\S+', re.I)


def human_error(err):
    """Понятное объяснение ошибки yt-dlp/ffmpeg; если случай незнакомый — исходный текст."""
    msg = str(err).replace('ERROR: ', '').strip()
    # «[youtube] dQw4w9WgXcQ: …» и адреса не сравниваем: в id и ссылках попадаются «paid», «ssl», «login», «404»
    bare = URL_IN_TEXT.sub('<url>', ERROR_PREFIX.sub('', msg))
    for rx, text in ERROR_RULES:
        if rx.search(bare):
            text, doh = str(text), str(DOH_HINT)
            # защищённый DNS уже включён — не советуем его включить
            return text.replace(doh, '') if doh in text and load_settings()['doh'] else text
    return msg


def error_detail(err, text):
    """Исходный текст ошибки — для «Подробнее», если объяснение его заменило."""
    msg = str(err).replace('ERROR: ', '').strip()
    return '' if msg == text else msg[:1500]


def error_response(err, status=400):
    text = human_error(err)
    return jsonify(error=text, detail=error_detail(err, text)), status


def fmt_size(num):
    if not num:
        return ''
    units = tr(('Б', 'КБ', 'МБ', 'ГБ', 'ТБ'), ('B', 'KB', 'MB', 'GB', 'TB'))
    for unit in units[:4]:
        if num < 1024:
            return f'~{num:.1f} {unit}' if unit == units[3] else f'~{num:.0f} {unit}'
        num /= 1024
    return f'~{num:.1f} {units[4]}'


def fsize(f, duration=None):
    size = f.get('filesize') or f.get('filesize_approx')
    if not size and f.get('tbr') and duration:  # оценка по битрейту (кбит/с)
        size = f['tbr'] * 1000 / 8 * duration
    return size or 0


def video_options(info):
    formats = info.get('formats') or []
    duration = info.get('duration')
    videos = [f for f in formats if f.get('vcodec') not in (None, 'none') and f.get('height')]
    audios = [f for f in formats if f.get('vcodec') == 'none' and f.get('acodec') not in (None, 'none')]
    best_audio = max(audios, key=lambda f: f.get('tbr') or 0, default=None)

    heights = sorted({f['height'] for f in videos}, reverse=True)
    if not heights and not formats:
        heights = PRESET_HEIGHTS
    options = []
    for h in heights:
        best = max((f for f in videos if f['height'] == h), key=lambda f: f.get('tbr') or 0)
        size = fsize(best, duration)
        if size and best.get('acodec') in (None, 'none') and best_audio:
            size += fsize(best_audio, duration)
        fps = best.get('fps')
        # вертикальное видео (TikTok, Reels, Shorts) 720×1280 — это «720p», а не «1280p»
        side = min(h, best.get('width') or h)
        label = f'{side}p' + (f' {int(fps)}fps' if fps and fps > 30 else '')
        options.append({'id': f'v{h}', 'kind': 'video', 'label': label, 'size': size,
                        'note': ' · '.join(x for x in ('MP4', fmt_size(size)) if x)})
    # только звук (SoundCloud и др.) — видео не предлагаем
    if not options and formats and any(f.get('vcodec') != 'none' for f in formats):
        options.append({'id': 'vbest', 'kind': 'video', 'label': tr('Видео', 'Video'), 'note': tr('лучшее качество', 'best quality')})
    return options


# ---------- звуковые дорожки (дубляж YouTube, фильмы с несколькими языками) ----------
LANG_ID = re.compile(r'[A-Za-z]{2,3}(-[A-Za-z0-9]{1,8}){0,3}')
LANG_NAMES = {
    'ru': 'Русский', 'en': 'Английский', 'uk': 'Украинский', 'be': 'Белорусский', 'kk': 'Казахский',
    'uz': 'Узбекский', 'ky': 'Киргизский', 'tg': 'Таджикский', 'tt': 'Татарский', 'hy': 'Армянский',
    'ka': 'Грузинский', 'az': 'Азербайджанский', 'de': 'Немецкий', 'fr': 'Французский', 'es': 'Испанский',
    'it': 'Итальянский', 'pt': 'Португальский', 'pl': 'Польский', 'cs': 'Чешский', 'sk': 'Словацкий',
    'bg': 'Болгарский', 'sr': 'Сербский', 'hr': 'Хорватский', 'sl': 'Словенский', 'ro': 'Румынский',
    'hu': 'Венгерский', 'el': 'Греческий', 'nl': 'Нидерландский', 'sv': 'Шведский', 'no': 'Норвежский',
    'nb': 'Норвежский', 'da': 'Датский', 'fi': 'Финский', 'et': 'Эстонский', 'lv': 'Латышский',
    'lt': 'Литовский', 'tr': 'Турецкий', 'ar': 'Арабский', 'he': 'Иврит', 'iw': 'Иврит', 'fa': 'Персидский',
    'hi': 'Хинди', 'bn': 'Бенгальский', 'ta': 'Тамильский', 'te': 'Телугу', 'ml': 'Малаялам', 'mr': 'Маратхи',
    'pa': 'Панджаби', 'gu': 'Гуджарати', 'kn': 'Каннада', 'ur': 'Урду', 'ja': 'Японский', 'ko': 'Корейский',
    'zh': 'Китайский', 'vi': 'Вьетнамский', 'th': 'Тайский', 'id': 'Индонезийский', 'ms': 'Малайский',
    'fil': 'Филиппинский', 'tl': 'Филиппинский',
}
REGION_NAMES = {'US': 'США', 'GB': 'Великобритания', 'AU': 'Австралия', 'CA': 'Канада', 'IN': 'Индия',
                'BR': 'Бразилия', 'PT': 'Португалия', 'ES': 'Испания', 'MX': 'Мексика', '419': 'Латинская Америка',
                'FR': 'Франция', 'Hans': 'упрощённый', 'Hant': 'традиционный', 'CN': 'Китай', 'TW': 'Тайвань'}
LANG_NAMES_EN = {
    'ru': 'Russian', 'en': 'English', 'uk': 'Ukrainian', 'be': 'Belarusian', 'kk': 'Kazakh',
    'uz': 'Uzbek', 'ky': 'Kyrgyz', 'tg': 'Tajik', 'tt': 'Tatar', 'hy': 'Armenian',
    'ka': 'Georgian', 'az': 'Azerbaijani', 'de': 'German', 'fr': 'French', 'es': 'Spanish',
    'it': 'Italian', 'pt': 'Portuguese', 'pl': 'Polish', 'cs': 'Czech', 'sk': 'Slovak',
    'bg': 'Bulgarian', 'sr': 'Serbian', 'hr': 'Croatian', 'sl': 'Slovenian', 'ro': 'Romanian',
    'hu': 'Hungarian', 'el': 'Greek', 'nl': 'Dutch', 'sv': 'Swedish', 'no': 'Norwegian',
    'nb': 'Norwegian', 'da': 'Danish', 'fi': 'Finnish', 'et': 'Estonian', 'lv': 'Latvian',
    'lt': 'Lithuanian', 'tr': 'Turkish', 'ar': 'Arabic', 'he': 'Hebrew', 'iw': 'Hebrew', 'fa': 'Persian',
    'hi': 'Hindi', 'bn': 'Bengali', 'ta': 'Tamil', 'te': 'Telugu', 'ml': 'Malayalam', 'mr': 'Marathi',
    'pa': 'Punjabi', 'gu': 'Gujarati', 'kn': 'Kannada', 'ur': 'Urdu', 'ja': 'Japanese', 'ko': 'Korean',
    'zh': 'Chinese', 'vi': 'Vietnamese', 'th': 'Thai', 'id': 'Indonesian', 'ms': 'Malay',
    'fil': 'Filipino', 'tl': 'Filipino',
}
REGION_NAMES_EN = {'US': 'US', 'GB': 'UK', '419': 'Latin America', 'Hans': 'Simplified', 'Hant': 'Traditional'}


def lang_label(code, note):
    """«ru» → «Русский», «en-US» → «Английский (США)»; неизвестный — как подписал сайт («Swahili»)."""
    base, *rest = code.split('-')
    name = tr(LANG_NAMES, LANG_NAMES_EN).get(base.lower())
    if not name:
        # YouTube: «Swahili original (default), medium» → «Swahili»
        name = re.split(r',| original| \(default\)', note or '')[0].strip() or code
        return name
    regions = [tr(REGION_NAMES, REGION_NAMES_EN).get(r, r) for r in rest]
    return name + (f' ({", ".join(regions)})' if regions else '')


def audio_tracks(info):
    """Дорожки на разных языках: [{id, label, original, size}]. Первая — какую качаем по умолчанию:
    русская, иначе оригинал. Одна дорожка (почти все видео) — пустой список, выбора в карточке нет."""
    duration = info.get('duration')
    tracks = {}
    for f in info.get('formats') or []:
        lang = f.get('language')
        if f.get('vcodec') != 'none' or f.get('acodec') in (None, 'none') or not lang or not LANG_ID.fullmatch(lang):
            continue
        note = f.get('format_note') or ''
        t = tracks.setdefault(lang, {'id': lang, 'note': note, 'original': False, 'size': 0})
        # YouTube помечает оригинал language_preference=10 и словом «original» в format_note
        if (f.get('language_preference') or 0) >= 10 or 'original' in note.lower():
            t['original'] = True
        t['size'] = max(t['size'], fsize(f, duration))
    if len(tracks) < 2:
        return []
    out = [{'id': t['id'], 'label': lang_label(t['id'], t['note']) + (tr(' — оригинал', ' — original') if t['original'] else ''),
            'original': t['original'], 'size': t['size']} for t in tracks.values()]
    if i18n.CURRENT == 'en':  # английский интерфейс — сначала оригинал, потом английская
        out.sort(key=lambda t: (not t['original'], not t['id'].lower().startswith('en'), t['label']))
    else:
        out.sort(key=lambda t: (not t['id'].lower().startswith('ru'), not t['original'], t['label']))
    return out


def audio_format(track, langs):
    """Какой звук взять: выбранный язык, все дорожки (langs) или, если не выбирали, русский, если он есть
    (в английском интерфейсе — дорожку по умолчанию, обычно оригинал)."""
    if track == 'all' and langs:
        return '+'.join(f'ba[language={lang}]' for lang in langs)
    if track:
        return f'ba[language={track}]'
    return tr('ba[language^=ru]', 'ba')


# ---------- субтитры ----------
SUB_ID = re.compile(r'[\w-]{1,40}')


def subtitle_list(info):
    """Субтитры: [{id, label, auto}], первыми — русские. Свои (subtitles) — все. Автоматические у YouTube —
    распознанный оригинал («en-orig») и автоперевод на 150 языков; из перевода берём только русский, когда
    русских нет (YouTube отдаёт его через раз, бывает HTTP 429 — тогда видео качается без субтитров).
    У других сайтов автоматических немного — показываем все."""
    def usable(d):
        return {k: v for k, v in (d or {}).items() if v and k != 'live_chat' and SUB_ID.fullmatch(k)}

    def name(formats):
        return next((f['name'] for f in formats if f.get('name')), '')

    subs = usable(info.get('subtitles'))
    out = [{'id': k, 'label': lang_label(k, name(v)), 'auto': False} for k, v in subs.items()]
    auto = usable(info.get('automatic_captions'))
    orig = [k for k in auto if k.endswith('-orig')]
    picks = orig or (list(auto) if len(auto) <= 10 else [k for k in ('ru', 'en') if k in auto])
    for k in picks:
        lang = k.removesuffix('-orig')
        if lang not in subs:  # свои субтитры на этом языке лучше распознанных
            out.append({'id': k, 'label': lang_label(lang, name(auto[k])) + tr(' — автоматические', ' — auto-generated'), 'auto': True})
    if orig and 'ru' in auto and not any(s['id'].lower().startswith('ru') for s in out):
        out.append({'id': 'ru', 'label': lang_label('ru', '') + tr(' — автоперевод', ' — auto-translated'), 'auto': True})
    main = (info.get('language') or '').split('-')[0].lower()
    out.sort(key=lambda s: (not s['id'].lower().startswith('ru'),
                            not (main and s['id'].lower().startswith(main)), s['auto'], s['label']))
    return out


def subtitle_opts(opts, subs):
    """Добавить к настройкам загрузки субтитры: subs = {lang, auto, embed}. Всегда .srt — его открывает
    любой плеер; вшитые в MP4 включаются в меню плеера, отдельный файл лежит рядом с видео."""
    opts['writeautomaticsub' if subs['auto'] else 'writesubtitles'] = True
    opts['subtitleslangs'] = [subs['lang']]
    opts['subtitlesformat'] = 'srt/best'  # YouTube отдаёт srt сам, остальное переводит ffmpeg
    pps = [{'key': 'FFmpegSubtitlesConvertor', 'format': 'srt', 'when': 'before_dl'}]
    if subs['embed']:
        pps.append({'key': 'FFmpegEmbedSubtitle', 'already_have_subtitle': False})  # .srt после вшивания удаляется
    opts['postprocessors'] = pps + opts.get('postprocessors', [])


def without_subtitles(opts):
    keys = ('writesubtitles', 'writeautomaticsub', 'subtitleslangs', 'subtitlesformat')
    out = {k: v for k, v in opts.items() if k not in keys}
    out['postprocessors'] = [p for p in opts.get('postprocessors', [])
                             if p['key'] not in ('FFmpegSubtitlesConvertor', 'FFmpegEmbedSubtitle')]
    return out


def audio_options():
    return [
        {'id': 'a_mp3', 'kind': 'audio', 'label': 'MP3', 'note': tr('лучшее качество', 'best quality')},
        {'id': 'a_m4a', 'kind': 'audio', 'label': 'M4A', 'note': tr('без перекодирования', 'no re-encoding')},
    ]


def clock(sec):
    """83.5 → '1:23', 3725 → '1:02:05'."""
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'


def download_opts(option, playlist, folder, section=None, track='', langs=(), subs=None):
    """section — (начало, конец) в секундах, если нужен только кусок видео.
    track — язык звуковой дорожки, 'all' — все дорожки из langs (только видео), пусто — русская, если есть.
    subs — субтитры {lang, auto, embed} (только видео целиком: к куску они бы не совпали по времени)."""
    opts = base_opts()
    opts['noplaylist'] = not playlist
    opts['windowsfilenames'] = True
    # двоеточие в имени файла Windows не пускает: «фрагмент 1.20-2.45»
    suffix = ''
    if section:
        start, end = section
        opts['download_ranges'] = download_range_func(None, [(start, end)])
        # точный разрез: кусок перекодируется, зато начинается ровно с 1:20. Без этого видео режется
        # по ключевым кадрам, а звук по своим блокам — MP3 выходил на 10 секунд длиннее
        opts['force_keyframes_at_cuts'] = True
        suffix = tr(' (фрагмент {}-{})', ' (clip {}-{})').format(clock(start), clock(end)).replace(':', '.')
    if playlist:
        name = '%(playlist_title).80B/%(playlist_index)03d - %(title).120B.%(ext)s'
    else:
        name = f'%(title).150B [%(id)s]{suffix}.%(ext)s'
    opts['outtmpl'] = str(folder / name)

    if option.startswith('v'):
        h = option[1:]
        audio = audio_format(track, langs)
        if h.isdigit():
            opts['format'] = f'bv*[height<={h}]+{audio}/bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b'
        else:
            opts['format'] = f'bv*+{audio}/bv*+ba/b'
        # при одинаковом разрешении — H.264 + AAC: открывается любым плеером
        opts['format_sort'] = ['res', 'vcodec:h264', 'acodec:aac', 'ext:mp4:m4a']
        opts['merge_output_format'] = 'mp4'
        if track == 'all' and langs:
            opts['allow_multiple_audio_streams'] = True
            # «по умолчанию» — только первая (русская или оригинал), иначе плеер выбирает какую хочет
            opts['postprocessor_args'] = {'merger+ffmpeg_o': ['-disposition:a', '0', '-disposition:a:0', 'default']}
            # язык у каждой дорожки — плеер (VLC, «Кино и ТВ») покажет «Русский», «English»…
            opts['postprocessors'] = [{'key': 'FFmpegMetadata', 'add_metadata': True}]
        if subs and not section:
            subtitle_opts(opts, subs)
    else:
        codec = option.split('_', 1)[1]
        audio = audio_format('' if track == 'all' else track, ())
        opts['format'] = (f'{audio}[ext=m4a]/{audio}/ba[ext=m4a]/ba/b' if codec == 'm4a'
                          else f'{audio}/ba/b')
        opts['writethumbnail'] = True
        if playlist:
            name = '%(playlist_title).80B/%(playlist_index)03d - %(artist,uploader&{} - |)s%(track,title).100B.%(ext)s'
        else:
            # без исполнителя и канала (ЯП файлы, простые сайты) — без «NA - » в начале
            # длину исполнителя ограничивает MetadataParser ниже: .60B здесь отрезал бы и « - »
            name = f'%(artist,uploader&{{}} - |)s%(track,title).120B{suffix}.%(ext)s'
        opts['outtmpl'] = str(folder / name)
        opts['postprocessors'] = [
            # «Исполнитель - Песня (Official Video)» → artist + track без мусора в скобках
            {'key': 'MetadataParser', 'when': 'pre_process', 'actions': [
                (MetadataParserPP.Actions.INTERPRET, 'title', r'(?P<artist>.+?)\s*[-–—]\s+(?P<track>.+)'),  # и «Цой- Песня»
                (MetadataParserPP.Actions.REPLACE, 'track', TITLE_JUNK, ''),
                (MetadataParserPP.Actions.REPLACE, 'title', TITLE_JUNK, ''),
                (MetadataParserPP.Actions.REPLACE, 'artist', ARTIST_CUT, r'\1'),
                (MetadataParserPP.Actions.REPLACE, 'uploader', ARTIST_CUT, r'\1'),
            ]},
            {'key': 'FFmpegExtractAudio', 'preferredcodec': codec, 'preferredquality': '0'},
            {'key': 'FFmpegMetadata', 'add_metadata': True},
            {'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg', 'when': 'before_dl'},
            {'key': 'EmbedThumbnail'},
        ]
    return opts


WIN_RESERVED = re.compile(r'(?i)^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])\s*(\..*)?$')
NAME_FIELDS = ('title', 'track', 'artist', 'uploader', 'playlist_title', 'playlist')


class SafeNamesPP(yt_dlp.postprocessor.PostProcessor):
    """Имена файлов и папок — из названий на сайте. yt-dlp заменяет / и \\, но пропускает «..»
    (плейлист с названием «..» сохранился бы на уровень выше папки загрузок) и имена устройств
    Windows (CON, NUL, COM1… — в такой «файл» не записать). Запускается перед выбором имени файла."""

    def __init__(self, downloader, folder):
        super().__init__(downloader)
        self.folder = Path(folder).resolve()

    def run(self, info):
        for key in NAME_FIELDS:
            value = info.get(key)
            if not isinstance(value, str):
                continue
            if value.strip() and not value.strip(' .'):
                info[key] = value.replace('.', '_')
            elif WIN_RESERVED.match(value.strip()):
                info[key] = value.strip() + '_'
        path = Path(self._downloader.prepare_filename(info)).resolve()
        if not path.is_relative_to(self.folder):
            raise yt_dlp.utils.DownloadError(tr('Имя файла выходит за папку загрузок: ', 'The file name points outside the download folder: ') + str(path))
        return [], info


class CoubLoopPP(yt_dlp.postprocessor.ffmpeg.FFmpegPostProcessor):
    """COUB: картинка — петля на несколько секунд, звук — целая песня. После склейки видео замирало
    на первом круге. loop — крутить видео по кругу до конца звука (как на сайте), иначе звук обрезается
    по видео. Без перекодирования: -stream_loop + -c copy, ~0,1 с на пятиминутный ролик."""

    def __init__(self, downloader, loop):
        super().__init__(downloader)
        self.loop = loop

    def run(self, info):
        path = info.get('filepath')
        if info.get('extractor_key') != 'Coub' or len(info.get('requested_formats') or ()) < 2 or not path:
            return [], info  # только склеенные видео + звук (MP3/M4A и кусок сюда не попадают)
        temp = yt_dlp.utils.prepend_extension(path, 'temp')
        self.to_screen('Зацикливаю видео под длину звука' if self.loop else 'Обрезаю звук по видео')
        self.real_run_ffmpeg([(path, ['-stream_loop', '-1'] if self.loop else []), (path, [])],
                             [(temp, ['-map', '0:v:0', '-map', '1:a:0', '-c', 'copy', '-shortest',
                                      '-movflags', '+faststart'])])
        os.replace(temp, path)
        return [], info


class Cancelled(Exception):
    pass


# ---------- прокси VPN-программы ----------
# VPN-расширение браузера меняет только сам браузер. Но многие VPN-программы для компьютера (v2rayN, Hiddify,
# NekoBox, Clash, Happ…) держат на компьютере прокси — через него программа и ходит на закрытые сайты.
# Порты, которые эти программы ставят по умолчанию (http — первым: ffmpeg умеет только его).
PROXY_PORTS = [10809, 10808, 2080, 12334, 7890, 7897, 2081, 1080, 9150, 9050]
PROXY_RE = re.compile(r'^(https?|socks5h?|socks4a?)://(\[[\da-f:]+\]|[\w.-]+):(\d{1,5})/?$', re.I)
YOUTUBE_SITE = re.compile(r'(^|\.)(youtube\.com|youtu\.be|youtube-nocookie\.com)$')
# так выглядит сайт, закрытый провайдером: соединение рвут, не отвечают или не находят адрес
BLOCKED_ERROR = re.compile(r'timed? ?out|connection (reset|aborted|refused)|remote end closed|winerror 100(54|60|61)|'
                           r'unexpected_eof|eof occurred in violation|getaddrinfo|failed to resolve|'
                           r'unable to connect|network is unreachable|'
                           r'http error 410|410: gone|'  # 410 — сайт ушёл из страны (PornHub)
                           r'ip address is blocked', re.I)  # TikTok: пост закрыт для страны
proxy_lock = threading.Lock()


def site_of(url):
    if url.startswith('ytsearch'):
        return 'youtube.com'
    host = urllib.parse.urlsplit(url if '://' in url else 'https://' + url).hostname or ''
    return host.lower().removeprefix('www.').removeprefix('m.')


def normalize_proxy(text):
    """«127.0.0.1:10808» → «http://127.0.0.1:10808»; None — если это не адрес прокси."""
    text = text.strip()
    if text and '://' not in text:
        text = 'http://' + text
    m = PROXY_RE.match(text)
    return text.rstrip('/') if m and 0 < int(m.group(3)) < 65536 else None


def proxy_alive(proxy):
    """Слушает ли прокси (VPN-программу могли выключить)."""
    p = urllib.parse.urlsplit(proxy)
    try:
        with socket.create_connection((p.hostname, p.port), timeout=1):
            return True
    except OSError:
        return False


def proxy_for(url):
    """Прокси для этой ссылки или None. Через прокси: YouTube, сайты, которые не открылись
    напрямую, или все сайты, если так выбрано. Прокси не отвечает — идём напрямую."""
    s = load_settings()
    if not s['proxy']:
        return None
    site = site_of(url)
    if s['proxy_all'] or YOUTUBE_SITE.search(site) or site in s['proxy_sites']:
        return s['proxy'] if proxy_alive(s['proxy']) else None
    return None


def proxy_works(proxy):
    """Открывается ли через прокси YouTube (закрытый сайт, по нему и проверяем)."""
    try:
        with yt_dlp.YoutubeDL({**base_opts(), 'proxy': proxy, 'socket_timeout': 8}) as ydl:
            ydl.urlopen(yt_dlp.networking.Request('https://www.youtube.com/generate_204')).close()
        return True
    except Exception:  # noqa: BLE001
        return False


def find_proxy():
    """Ищет прокси VPN-программы на этом компьютере (по портам, которые они ставят по умолчанию)."""
    for port in PROXY_PORTS:
        with socket.socket() as sock:
            sock.settimeout(0.3)
            if sock.connect_ex(('127.0.0.1', port)) != 0:
                continue
        for scheme in ('http', 'socks5h'):
            proxy = f'{scheme}://127.0.0.1:{port}'
            if proxy_works(proxy):
                return proxy
    return None


def via_proxy(url, work):
    """work(proxy) обращается к сайту через yt-dlp (proxy — адрес или None).
    Сайт не открылся напрямую (похоже на блокировку) — пробуем ещё раз через прокси: заданный в настройках
    или найденный на компьютере. Получилось — запоминаем сайт (и прокси): дальше сразу через него."""
    proxy = proxy_for(url)
    try:
        return work(proxy)
    except Exception as e:  # noqa: BLE001
        if proxy or not BLOCKED_ERROR.search(str(e)):
            raise
        with proxy_lock:  # несколько загрузок разом не ищут прокси одновременно
            saved = load_settings()['proxy']
            proxy = saved if saved and proxy_alive(saved) else find_proxy()
        if not proxy:
            raise
    result = work(proxy)
    with proxy_lock:
        s = load_settings()
        save_settings(proxy=proxy, proxy_sites=sorted(set(s['proxy_sites']) | {site_of(url)}))
    return result


# Защищённый DNS. Провайдер может прятать адреса закрытых сайтов (getaddrinfo failed), а zapret и т. п. только
# пропускают соединение. С галочкой адрес, который не нашёл системный DNS, спрашиваем у Cloudflare/Google
# по HTTPS — по IP, самим серверам DNS не нужен (Quad9 по HTTP/1.1 не отвечает). Действует на всё, что ходит
# через socket (yt-dlp, прослушивание, поиск); ffmpeg (кусок) и curl_cffi (TikTok, Instagram) ищут адреса сами.
DOH_SERVERS = ['https://1.1.1.1/dns-query', 'https://8.8.8.8/resolve',
               'https://1.0.0.1/dns-query', 'https://8.8.4.4/resolve']
system_getaddrinfo = socket.getaddrinfo
doh_cache = {}  # (имя, тип записи) → (адреса, до какого времени)


def doh_lookup(host, rtype=1):
    """Адреса имени (rtype 1 — IPv4, 28 — IPv6) через DNS по HTTPS; [] — нет такого имени или серверы молчат."""
    key = (host, rtype)
    hit = doh_cache.get(key)
    if hit and hit[1] > time.time():
        return hit[0]
    for server in DOH_SERVERS:
        req = urllib.request.Request(f'{server}?name={urllib.parse.quote(host)}&type={rtype}',
                                     headers={'Accept': 'application/dns-json'})
        try:
            with urllib.request.urlopen(req, timeout=4) as r:
                answers = [a for a in json.load(r).get('Answer', []) if a.get('type') == rtype]
        except (OSError, ValueError, AttributeError):
            continue
        ttl = min([a.get('TTL', 300) for a in answers] or [60])
        doh_cache[key] = [a['data'] for a in answers], time.time() + min(max(ttl, 60), 3600)
        return doh_cache[key][0]
    return []


def getaddrinfo_doh(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002 — как у socket.getaddrinfo
    """socket.getaddrinfo: системный DNS, а если он не нашёл имя и включён защищённый DNS — DNS по HTTPS."""
    try:
        return system_getaddrinfo(host, port, family, type, proto, flags)
    except socket.gaierror:
        name = (host.decode() if isinstance(host, bytes) else host or '').rstrip('.').lower()
        try:
            name = name.encode('idna').decode() if '.' in name and load_settings()['doh'] else ''
        except UnicodeError:
            name = ''
        ips = doh_lookup(name, 1) if name and family != socket.AF_INET6 else []
        if name and not ips and family != socket.AF_INET:
            ips = doh_lookup(name, 28)
        if not ips:
            raise
        return [info for ip in ips
                for info in system_getaddrinfo(ip, port, family, type, proto, flags | socket.AI_NUMERICHOST)]


socket.getaddrinfo = getaddrinfo_doh


def remove_partial(task):
    """Удаляет недокачанные куски (.part, фрагменты HLS) после отмены."""
    gc.collect()  # закрываем брошенные файловые дескрипторы, иначе Windows не даст удалить
    time.sleep(1)
    for name in task['tmp']:
        path = Path(name)
        for f in path.parent.glob(glob_escape(path.name) + '*'):
            if '.part' in f.name or f.suffix == '.ytdl':
                try:
                    f.unlink()
                except OSError:
                    pass


def glob_escape(name):
    return ''.join(f'[{c}]' if c in '[]*?' else c for c in name)


def run_task(task):
    def progress(d):
        if task['cancel']:
            raise Cancelled()
        info = d.get('info_dict') or {}
        if d.get('filename'):
            task['tmp'].add(d['filename'])
        if info.get('playlist_index') and task.get('count'):
            task['item'] = tr('{} из {}', '{} of {}').format(info['playlist_index'], task['count'])
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate')
            done = d.get('downloaded_bytes') or 0
            if total:
                task['percent'] = round(done / total * 100, 1)
            elif d.get('fragment_count'):
                task['percent'] = round((d.get('fragment_index') or 0) / d['fragment_count'] * 100, 1)
            is_audio = info.get('vcodec') == 'none'
            task['stage'] = (tr('Скачиваю звук', 'Downloading audio') if is_audio and task['kind'] == 'video'
                             else tr('Скачиваю', 'Downloading'))
            task['speed'] = d.get('_speed_str', '').strip()
            task['eta'] = d.get('_eta_str', '').strip()
        elif d['status'] == 'finished':
            task['percent'] = 100

    def pp_progress(d):
        if task['cancel']:
            raise Cancelled()
        if d['status'] == 'started' and d.get('postprocessor') != 'SafeNames':  # проверка имён — не обработка
            task['stage'] = tr('Обработка', 'Processing')
            task['speed'] = task['eta'] = ''

    def got_info(info, incomplete=False):
        if task['from_list'] and info.get('title') and not incomplete:
            task['title'] = info['title']  # в списке ссылок название узнаём только при загрузке
        if task['section']:
            task['stage'] = tr('Скачиваю фрагмент', 'Downloading clip')  # ffmpeg не сообщает проценты — полоса просто бежит
        return None

    task_of_thread[threading.get_ident()] = task
    try:
        opts = download_opts(task['option'], task['playlist'], Path(task['dir']), task['section'],
                             task['track'], task['langs'], task['subs'])
        opts['match_filter'] = got_info  # вызывается, когда сведения о видео уже получены
        opts['progress_hooks'] = [progress]
        opts['postprocessor_hooks'] = [pp_progress]
        opts['post_hooks'] = [lambda path: task['files'].append(path)]

        def run(o):
            with yt_dlp.YoutubeDL(o) as ydl:
                ydl.add_post_processor(SafeNamesPP(ydl, task['dir']), when='video')
                if task['kind'] == 'video' and not task['section']:
                    ydl.add_post_processor(CoubLoopPP(ydl, task['loop']), when='post_process')
                ydl.download([task['url']])

        def work(proxy):
            o = {**opts, 'proxy': proxy} if proxy else opts
            try:
                run(o)
            except yt_dlp.utils.DownloadError as e:
                # субтитры качаются до видео: не скачались (429 и т. п.) — видео всё равно нужно
                if not o.get('subtitleslangs') or 'Unable to download video subtitles' not in str(e):
                    raise
                run(without_subtitles(o))
                task['label'] += tr(' · без субтитров (сайт их не отдал)', ' · no subtitles (the site did not give them)')

        via_proxy(task['url'], work)
        task['status'] = 'done'
        task['stage'] = tr('Готово', 'Done')
        task['percent'] = 100
    except Cancelled:
        task['status'] = 'cancelled'
        task['stage'] = tr('Отменено', 'Cancelled')
    except Exception as e:  # noqa: BLE001 — показываем пользователю любую ошибку
        if task['cancel']:
            task['status'], task['stage'] = 'cancelled', tr('Отменено', 'Cancelled')
        else:
            task['status'] = 'error'
            task['stage'] = tr('Ошибка', 'Error')
            task['error'] = human_error(e)
            task['error_detail'] = error_detail(e, task['error'])
    task['speed'] = task['eta'] = ''
    task_of_thread.pop(threading.get_ident(), None)
    task['procs'].clear()
    # чистим вне except: пока жив traceback, недокачанный файл остаётся открытым
    if task['status'] == 'cancelled':
        remove_partial(task)


def page(name):
    """Страница на языке программы: help.html или help.en.html; в index.html — <html lang> (тексты — web/i18n.js)."""
    if i18n.CURRENT == 'en' and (WEB / name.replace('.html', '.en.html')).exists():
        name = name.replace('.html', '.en.html')
    html = (WEB / name).read_text(encoding='utf-8')
    if i18n.CURRENT == 'en':
        html = html.replace('<html lang="ru">', '<html lang="en">', 1).replace(
            '<title>Универсальный скачиватель</title>', '<title>Universal Downloader</title>', 1)
    resp = Response(html, mimetype='text/html')
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


@app.get('/')
def index():
    return page('index.html')


@app.get('/help')
def help_page():
    return page('help.html')


@app.get('/i18n.js')
def i18n_js():
    return send_from_directory(WEB, 'i18n.js')


@app.get('/favicon.ico')
def favicon():
    return send_from_directory(WEB, 'icon.ico')


@app.get('/quick.js')
def quick_js():
    return send_from_directory(WEB, 'quick.js')


@app.get('/browsers.svg')
def browsers_svg():
    return send_from_directory(WEB, 'browsers.svg')


# английские тексты расширения: архив собирается на языке программы (меню — в background.js по UDL_LANG)
EXTENSION_EN = {
    'manifest.json': [
        ('"name": "Универсальный скачиватель"', '"name": "Universal Downloader"'),
        ('"short_name": "Скачиватель"', '"short_name": "Downloader"'),
        ('"description": "Кнопка и пункт меню «Скачать в программе»: открывает «Универсальный скачиватель» с текущей ссылкой."',
         '"description": "A button and the “Send to Downloader” menu item: opens Universal Downloader with the current link."'),
        ('"default_title": "Скачать в программе"', '"default_title": "Send to Downloader"'),
    ],
    'launch.html': [
        ('<html lang="ru">', '<html lang="en">'),
        ('<title>Запускаю скачиватель…</title>', '<title>Starting the downloader…</title>'),
        ('alt="">Универсальный скачиватель</h1>', 'alt="">Universal Downloader</h1>'),
        ('Программа не была запущена — запускаю. Как только она заработает, эта вкладка сама откроет её. На медленном '
         'компьютере это бывает до минуты — у часов видна надпись «Запускается…», нажимать ещё раз не нужно.',
         'The program was not running — starting it. As soon as it works, this tab opens it by itself. On a slow '
         'computer this takes up to a minute — “Starting…” is shown by the clock, no need to click again.'),
        ('Браузер спросит, открыть ли приложение, — нажмите <b>«Открыть»</b>. Галочка «Всегда разрешать» уберёт этот вопрос.',
         'The browser asks whether to open the app — click <b>“Open”</b>. The “Always allow” checkbox removes this question.'),
        ('>Запустить ещё раз</button>', '>Start again</button>'),
        ('Не запускается? Запустите программу ярлыком <b>«Универсальный скачиватель»</b> на рабочем столе — эта вкладка\n'
         '      перейдёт в неё сама. Если программа старой версии, после такого запуска кнопка в браузере будет включать её сама.',
         'Does not start? Start the program with the <b>“Универсальный скачиватель”</b> (Universal Downloader) desktop shortcut — '
         'this tab\n      switches to it by itself.'),
    ],
}


@app.get('/extension.zip')
def extension_zip():
    """Расширение для Chrome/Edge/Яндекса/Firefox. Файлы — в корне архива: «Извлечь всё» даёт папку
    сразу с manifest.json. В config.js подставляем порт, на котором работает программа."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for f in sorted(EXTENSION.rglob('*')):
            if f.is_file():
                name = f.relative_to(EXTENSION).as_posix()
                if name == 'config.js':
                    data = f"self.UDL_PORT = {PORT};\nself.UDL_LANG = '{i18n.CURRENT}';\n".encode()
                elif name in EXTENSION_EN and i18n.CURRENT == 'en':
                    text = f.read_text(encoding='utf-8')
                    for ru, en in EXTENSION_EN[name]:
                        text = text.replace(ru, en)
                    data = text.encode()
                else:
                    data = f.read_bytes()
                z.writestr(name, data)
    return Response(buf.getvalue(), mimetype='application/zip',
                    headers={'Content-Disposition': 'attachment; filename="downloader-extension.zip"', 'Cache-Control': 'no-store'})


def program_version(name):
    """Версия внешней программы (node, deno) или None, если её нет."""
    path = shutil.which(name)
    if not path:
        return None
    try:
        out = subprocess.run([path, '--version'], capture_output=True, text=True, timeout=10)
        return out.stdout.strip().splitlines()[0] if out.stdout.strip() else 'есть'
    except (OSError, subprocess.SubprocessError):
        return 'есть'


@app.get('/api/status')
def api_status():
    ffmpeg = BIN / ('ffmpeg.exe' if os.name == 'nt' else 'ffmpeg')
    return jsonify(
        version=VERSION,
        ytdlp=yt_dlp.version.__version__,
        python=sys.version.split()[0],
        ffmpeg=ffmpeg.exists(),
        node=program_version('node'),
        deno=program_version('deno'),
        cookies=COOKIES.exists(),
        downloads=str(download_dir()),
    )


@app.post('/api/update')
def api_update():
    """Обновляет yt-dlp. Новая версия начнёт работать после перезапуска программы."""
    try:
        out = subprocess.run(
            [sys.executable, '-m', 'pip', 'install', '-U', '--disable-pip-version-check', 'yt-dlp[default,curl-cffi]'],
            capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        return jsonify(ok=False, error=str(e)), 500
    if out.returncode != 0:
        tail = (out.stderr or out.stdout).strip().splitlines()[-3:]
        return jsonify(ok=False, error='\n'.join(tail) or tr('pip завершился с ошибкой', 'pip failed')), 500
    new = subprocess.run(
        [sys.executable, '-c', 'import yt_dlp; print(yt_dlp.version.__version__)'],
        capture_output=True, text=True, timeout=60).stdout.strip()
    return jsonify(ok=True, before=yt_dlp.version.__version__, after=new)


# Обновление самой программы (подробности — в updater.py).
# state: idle → downloading → restarting (программа закрывается) или error
app_update = {'state': 'idle', 'error': '', 'done': 0, 'total': 0, 'latest': None, 'result': None}


def github_open(url):
    """Запрос к GitHub сетью yt-dlp: если GitHub не открылся напрямую, via_proxy повторит через прокси."""
    def work(proxy):
        ydl = yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True, 'socket_timeout': 20, **({'proxy': proxy} if proxy else {})})
        headers = {'User-Agent': f'universal-downloader/{VERSION}'}
        if '//api.github.com/' in url:
            headers['Accept'] = 'application/vnd.github+json'
        return ydl.urlopen(yt_dlp.networking.Request(url, headers=headers))
    return via_proxy(url, work)


def read_update_cache():
    try:
        return json.loads(UPDATE_CACHE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def check_app_update(notify_new=False):
    """Спрашивает GitHub о новой версии. Нашлась — помним и в файле: плашка видна и после перезапуска."""
    updater.LANG = i18n.CURRENT
    info = updater.check(VERSION, github_open, UPDATE_API)
    app_update['latest'] = info
    cache = read_update_cache()
    cache.update(checked=time.time(), latest=info)
    if info and notify_new and cache.get('notified') != info['version']:
        cache['notified'] = info['version']
        notify(tr('Вышла версия {}. Обновить можно на странице программы — одной кнопкой.',
                   'Version {} is out. You can update on the program page with one click.').format(info['version']))
    try:
        UPDATE_CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding='utf-8')
    except OSError:
        pass
    tell_pages('update')
    if tray_icon is not None:
        tray_icon.update_menu()  # пункт «Обновить до …»
    return info


def check_app_update_regularly():
    """Раз в три дня. Программа может работать неделями, поэтому проверяем по кругу, а не только при запуске."""
    while True:
        if time.time() - read_update_cache().get('checked', 0) >= UPDATE_EVERY:
            try:
                check_app_update(notify_new=True)
            except Exception as e:  # noqa: BLE001 — нет интернета: попробуем через 6 часов
                print('Не удалось проверить обновление программы:', e)
        time.sleep(6 * 3600)


def load_app_update():
    """При запуске: что нашли в прошлый раз и чем кончилось обновление (итог пишет updater.py)."""
    latest = read_update_cache().get('latest')
    if latest and (updater.parse_version(latest.get('version')) or ()) > updater.parse_version(VERSION):
        app_update['latest'] = latest
    result = UPDATE_DIR / 'result.json'
    try:
        app_update['result'] = json.loads(result.read_text(encoding='utf-8'))
        result.unlink()
    except (OSError, ValueError):
        pass


def install_app_update():
    """Качает и проверяет новую версию, запускает updater.py и закрывает программу: файлы он заменит сам."""
    info = app_update['latest']
    try:
        updater.LANG = i18n.CURRENT
        root = updater.download(info, github_open, UPDATE_DIR, lambda done, total: app_update.update(done=done, total=total))
        script = UPDATE_DIR / 'updater.py'  # копия: свой файл updater.py тоже заменится
        shutil.copy2(BASE / 'updater.py', script)
        subprocess.Popen([sys.executable, str(script), '--base', str(BASE), '--work', str(UPDATE_DIR),
                          '--new', str(root), '--version', info['version'], '--old-version', VERSION,
                          '--pid', str(os.getpid()), '--port', str(PORT), '--lang', i18n.CURRENT],
                         cwd=UPDATE_DIR, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except ValueError as e:
        app_update.update(state='error', error=str(e))
        return
    except Exception as e:  # noqa: BLE001
        app_update.update(state='error', error=tr('не удалось скачать обновление: ', 'could not download the update: ') + str(e))
        return
    app_update['state'] = 'restarting'
    print(f'Обновление до {info["version"]}: программа перезапускается')
    time.sleep(1.5)  # страница успевает узнать, что программа перезапускается
    if tray_icon is not None:
        tray_icon.stop()  # иначе значок останется у часов до наведения мыши
    os._exit(0)


def app_update_state():
    return {**app_update, 'version': VERSION, 'dev': DEV_COPY}


@app.get('/api/app-update')
def api_app_update():
    return jsonify(app_update_state())


@app.post('/api/app-update/check')
def api_app_update_check():
    try:
        check_app_update()
    except Exception as e:  # noqa: BLE001
        return jsonify(error=tr('Не удалось связаться с GitHub: ', 'Could not reach GitHub: ') + str(e)), 502
    return jsonify(app_update_state())


@app.post('/api/app-update')
def api_app_update_install():
    if DEV_COPY:
        return jsonify(error=tr('Это папка разработки (git) — обновляйте её через git pull.',
                                 'This is a development folder (git) — update it with git pull.')), 409
    if not app_update['latest']:
        return jsonify(error=tr('Новой версии нет.', 'No new version.')), 400
    if app_update['state'] in ('downloading', 'restarting'):
        return jsonify(app_update_state())
    if any(t['status'] in ('running', 'queued') for t in list(tasks.values())):
        return jsonify(error=tr('Сначала дождитесь конца загрузок или отмените их: при обновлении программа перезапускается.',
                                 'Wait for downloads to finish or cancel them first: the program restarts when updating.')), 409
    app_update.update(state='downloading', error='', done=0, total=app_update['latest'].get('size') or 0)
    threading.Thread(target=install_app_update, daemon=True).start()
    return jsonify(app_update_state())


URL_RE = re.compile(r'^(https?://)?([\w-]+\.)+[a-z]{2,}(:\d+)?(/|$)', re.I)


SEARCH_TIMEOUT = 25  # сайт, который молчит дольше, в эту выдачу не попадёт


def search_youtube(query, count):
    target = f'ytsearch{count}:{query}'

    def work(proxy):
        opts = {**base_opts(), 'extract_flat': 'in_playlist', 'proxy': proxy}
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(target, download=False)

    return [{
        'id': f"youtube:{e['id']}",
        'url': f"https://www.youtube.com/watch?v={e['id']}",
        'title': e.get('title') or '',
        'uploader': e.get('channel') or e.get('uploader') or '',
        'duration': e.get('duration'),
        'thumbnail': f"https://i.ytimg.com/vi/{e['id']}/mqdefault.jpg",
    } for e in via_proxy(target, work).get('entries') or [] if e and e.get('id')]


def search_soundcloud(query, count):
    def work(proxy):
        opts = {**base_opts(), 'extract_flat': 'in_playlist', 'proxy': proxy}
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(f'scsearch{count}:{query}', download=False)

    results = []
    for e in via_proxy('https://soundcloud.com/', work).get('entries') or []:
        # ровно 30 секунд — отрывок платного трека (SoundCloud Go+): целиком его не скачать
        if not e or not e.get('webpage_url') or abs((e.get('duration') or 0) - 30) < 1:
            continue
        thumbs = {t.get('id'): t.get('url') for t in e.get('thumbnails') or []}
        results.append({
            'id': f"soundcloud:{e['id']}",
            'url': e['webpage_url'],
            'title': e.get('title') or '',
            'uploader': e.get('uploader') or '',
            'duration': e.get('duration'),
            'thumbnail': thumbs.get('t300x300') or thumbs.get('large') or '',
        })
    return results


def search_rutube(query, count):
    """Открытый поиск Rutube (тот же, что на сайте); вход не нужен."""
    def work(proxy):
        with yt_dlp.YoutubeDL({**base_opts(), 'proxy': proxy}) as ydl:
            url = 'https://rutube.ru/api/search/video/?' + urllib.parse.urlencode({'query': query})
            return json.loads(ydl.urlopen(url).read())

    hidden = ('is_paid', 'is_locked', 'is_hidden', 'is_deleted', 'is_livestream', 'is_adult')
    return [{
        'id': f"rutube:{e['id']}",
        'url': e['video_url'],
        'title': e.get('title') or '',
        'uploader': (e.get('author') or {}).get('name') or '',
        'duration': e.get('duration'),
        'thumbnail': f"{e['thumbnail_url']}?width=320" if e.get('thumbnail_url') else '',  # без width — до 1600 px
    } for e in via_proxy('https://rutube.ru/', work).get('results') or []
        if isinstance(e, dict) and e.get('id') and e.get('video_url') and not any(e.get(k) for k in hidden)][:count]


SEARCH_SITES = {'youtube': ('YouTube', search_youtube), 'soundcloud': ('SoundCloud', search_soundcloud),
                'rutube': ('Rutube', search_rutube)}


def search(query, exclude=()):
    """Поиск песни сразу на нескольких сайтах — один общий список, сайты по очереди: YouTube, SoundCloud, Rutube.
    Сайт, который не открылся или долго молчит, просто не попадает в выдачу. Возвращает (песни, не ответившие сайты).

    exclude — id уже показанных («youtube:…»). YouTube на каждый запрос слегка меняет
    порядок выдачи, поэтому «следующие 10» — это первые 10 ещё не показанных."""
    exclude = set(exclude)
    pool = concurrent.futures.ThreadPoolExecutor(len(SEARCH_SITES))
    jobs = {}
    for key, (name, find) in SEARCH_SITES.items():
        shown = sum(x.startswith(key + ':') for x in exclude)
        jobs[key] = pool.submit(find, query, shown + 2 * SEARCH_LIMIT)
    pool.shutdown(wait=False)  # не дождались медленного сайта — его поток закончит сам
    done, _ = concurrent.futures.wait(jobs.values(), timeout=SEARCH_TIMEOUT)
    lists, failed, errors = [], [], []
    for key, job in jobs.items():
        name = SEARCH_SITES[key][0]
        if job not in done:
            failed.append(name)
            continue
        try:
            lists.append([dict(r, site=name) for r in job.result() if r['id'] not in exclude])
        except Exception as e:  # noqa: BLE001
            print(f'Поиск на {name} не удался:', e)
            failed.append(name)
            errors.append(e)
    if not any(lists) and failed:  # «ничего не найдено» было бы неправдой: сайт просто не ответил
        raise errors[0] if errors else TimeoutError('timed out')
    results = []
    for row in itertools.zip_longest(*lists):
        for r in row:
            if r and r['id'] not in exclude and len(results) < SEARCH_LIMIT:
                exclude.add(r['id'])
                results.append(r)
    return results, failed


@app.post('/api/info')
def api_info():
    data = json_body()
    url = text_arg(data, 'url')
    if not url:
        return jsonify(error=tr('Вставьте ссылку или напишите название песни.', 'Paste a link or type a song name.')), 400
    if not URL_RE.match(url):
        exclude = [x for x in data.get('exclude') or [] if isinstance(x, str)][:500]
        try:
            results, failed = search(url, exclude)
        except Exception as e:  # noqa: BLE001
            return error_response(e)
        if not results and not exclude:
            return jsonify(error=tr('Ничего не найдено. Попробуйте написать иначе.', 'Nothing found. Try wording it differently.')), 404
        return jsonify(search=True, query=url, append=bool(exclude), results=results,
                       more=len(results) == SEARCH_LIMIT, failed=failed)
    def work(proxy):
        opts = {**base_opts(), 'extract_flat': 'in_playlist', 'proxy': proxy}
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info = via_proxy(url, work)
    except Exception as e:  # noqa: BLE001
        return error_response(e)

    playlist = info.get('_type') == 'playlist'
    if playlist:
        entries = [e for e in (info.get('entries') or []) if e]
        thumb = info.get('thumbnail') or next(
            ((e.get('thumbnails') or [{}])[-1].get('url') for e in entries if e.get('thumbnails')), None)
        options = [{'id': f'v{h}', 'kind': 'video', 'label': tr('до {}p', 'up to {}p').format(h), 'note': 'MP4'}
                   for h in (1080, 720, 480)]
        return jsonify(
            url=url, playlist=True, count=len(entries),
            title=info.get('title') or tr('Плейлист', 'Playlist'),
            site=info.get('extractor_key') or info.get('webpage_url_domain'),
            uploader=info.get('uploader') or info.get('channel'),
            thumbnail=thumb, duration=None,
            options=options + audio_options(),
            choices=info.get('udl_choices'),
        )

    return jsonify(
        url=info.get('webpage_url') or url, playlist=False, count=1,
        title=info.get('title') or tr('Без названия', 'Untitled'),
        site=info.get('extractor_key') or info.get('webpage_url_domain'),
        uploader=info.get('uploader') or info.get('channel'),
        thumbnail=info.get('thumbnail'),
        duration=info.get('duration'),
        live=bool(info.get('is_live')),
        options=video_options(info) + audio_options(),
        tracks=audio_tracks(info),
        subtitles=subtitle_list(info),
        loop=coub_loop(info),
        choices=info.get('udl_choices'),  # плеер / сезон / серия (sites.BalancerIE) — выбор на странице
    )


def coub_loop(info):
    """COUB: видео — петля на несколько секунд, звук — целая песня. Тогда {video, audio} в секундах — на странице
    выбор «по кругу / один раз», иначе None. Длины звука в API нет; MP3 «med» у COUB — 128 кбит/с, считаем по размеру."""
    if info.get('extractor_key') != 'Coub' or not info.get('duration'):
        return None
    med = next((f for f in info.get('formats') or () if f.get('format_id') == 'html5-audio-med'), {})
    audio = round(med['filesize'] / 16000, 1) if med.get('filesize') else None
    if not audio or audio < info['duration'] + 1:
        return None
    return {'video': info['duration'], 'audio': audio}


# ---------- прослушивание ----------
# Звук отдаём через себя: прямые ссылки googlevideo привязаны к IP и заголовкам yt-dlp,
# а webm/opus Safari и Firefox играют по-разному — поэтому сначала m4a (AAC), он играет везде.
# Только «цельные» файлы по http(s): HLS/DASH-кусочки так не перемотать.
STREAM_FORMAT = ('bestaudio[ext=m4a][protocol^=http]/bestaudio[acodec^=mp4a][protocol^=http]'
                 '/bestaudio[protocol^=http]/best[protocol^=http]')
STREAM_TTL = 300        # найденную ссылку на поток помним 5 минут после последнего обращения
STREAM_ERROR_TTL = 60   # и ошибку — минуту, чтобы /api/listen и /api/stream не искали дважды
STREAM_CHUNK = 10 << 20  # у сайта просим куски по 10 МБ: целиком YouTube отдаёт медленно
STREAM_MIME = {'m4a': 'audio/mp4', 'mp4': 'audio/mp4', 'aac': 'audio/aac', 'mp3': 'audio/mpeg',
               'webm': 'audio/webm', 'weba': 'audio/webm', 'ogg': 'audio/ogg', 'opus': 'audio/ogg',
               'flac': 'audio/flac', 'wav': 'audio/wav'}
# звук только кусочками (HLS: SoundCloud, Rutube…) скачиваем целиком сюда; папка своя у каждого порта,
# чтобы тестовый запуск не удалял файлы запущенной программы
LISTEN_DIR = Path(tempfile.gettempdir()) / f'udl-listen-{PORT}'
LISTEN_FORMAT = 'bestaudio[acodec^=mp4a]/bestaudio/worst[acodec^=mp4a]/worst[acodec!=none]'
LISTEN_MAX = 40 * 60     # длиннее — скачивать для прослушивания слишком долго
streams = {}             # ссылка на страницу → что нашёл yt-dlp
streams_lock = threading.Lock()
stream_locks = {}        # своя блокировка на каждую ссылку: два запроса подряд ждут один поиск


class StreamError(Exception):
    pass


def resolve_stream(url):
    def work(proxy):
        # плейлист не разбираем целиком — его всё равно не послушать
        opts = {**base_opts(), 'format': STREAM_FORMAT, 'extract_flat': 'in_playlist', 'proxy': proxy}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info.get('_type') == 'playlist' or info.get('entries'):
                raise StreamError(tr('Послушать можно одну песню, а это плейлист.', 'You can listen to one song, and this is a playlist.'))
            if info.get('is_live'):
                raise StreamError(tr('Прямой эфир здесь не послушать.', 'Live streams cannot be played here.'))
            src = info.get('url')
            return info, proxy, (ydl.cookiejar.get_cookie_header(src) if src else '')

    try:
        info, proxy, cookie = via_proxy(url, work)
        src = info.get('url')
        headers = dict(info.get('http_headers') or {})
    except StreamError:
        raise
    except Exception as e:  # noqa: BLE001
        if 'requested format' in str(e).lower():
            return listen_copy(url)
        raise StreamError(human_error(e)) from None
    if not src:
        raise StreamError(tr('Не нашёл звук по этой ссылке.', 'No audio found at this link.'))
    # ссылка YouTube (клиент TV) открывается только через несколько секунд, до этого — 403;
    # при скачивании yt-dlp ждёт сам (available_at), для прослушивания ждём здесь
    delay = (info.get('available_at') or 0) - time.time()
    if delay > 0:
        time.sleep(min(delay, 20))
    if cookie:
        headers['Cookie'] = cookie
    headers.pop('Range', None)
    return {
        'src': src, 'headers': headers, 'size': info.get('filesize'), 'proxy': proxy,
        'mime': STREAM_MIME.get(info.get('ext'), 'application/octet-stream'),
        'meta': {
            'title': info.get('title') or '', 'uploader': info.get('uploader') or info.get('channel') or '',
            'duration': info.get('duration'), 'thumbnail': info.get('thumbnail'), 'ext': info.get('ext'),
        },
    }


def listen_copy(url):
    """Звук приходит только кусочками (HLS) — через Range его не отдать. Скачиваем песню целиком
    во временную папку (обычно несколько секунд) и отдаём файл: так работает и перемотка."""
    LISTEN_DIR.mkdir(exist_ok=True)
    folder = Path(tempfile.mkdtemp(dir=LISTEN_DIR))

    def work(proxy):
        opts = {**base_opts(), 'format': LISTEN_FORMAT, 'proxy': proxy, 'concurrent_fragment_downloads': 8,
                'outtmpl': str(folder / 'src.%(ext)s')}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info.get('_type') == 'playlist' or info.get('entries'):
                raise StreamError(tr('Послушать можно одну песню, а это плейлист.', 'You can listen to one song, and this is a playlist.'))
            if info.get('is_live'):
                raise StreamError(tr('Прямой эфир здесь не послушать.', 'Live streams cannot be played here.'))
            if (info.get('duration') or 0) > LISTEN_MAX:
                raise StreamError(tr('Запись длиннее {} минут с этого сайта не послушать — её можно скачать.',
                                     'Recordings longer than {} minutes from this site cannot be played here — download it instead.')
                                  .format(LISTEN_MAX // 60))
            ydl.process_ie_result(info, download=True)
            return info

    try:
        try:
            info = via_proxy(url, work)
        except StreamError:
            raise
        except Exception as e:  # noqa: BLE001
            raise StreamError(human_error(e)) from None
        src = next((f for f in folder.glob('src.*') if not f.name.endswith('.part')), None)
        if not src:
            raise StreamError(tr('Не нашёл звук по этой ссылке.', 'No audio found at this link.'))
        out = folder / 'listen.m4a'
        for audio in (['-c:a', 'copy'], ['-c:a', 'aac', '-b:a', '160k']):  # не всякий звук ложится в M4A как есть
            done = subprocess.run([str(FFMPEG), '-hide_banner', '-nostdin', '-y', '-loglevel', 'error', '-i', str(src),
                                   '-vn', *audio, '-movflags', '+faststart', str(out)], capture_output=True)
            if done.returncode == 0:
                break
        else:
            raise StreamError(tr('Не получилось подготовить звук для прослушивания — скачайте файл.',
                               'Could not prepare the audio for listening — download the file.'))
        src.unlink(missing_ok=True)
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return {
        'file': str(out), 'folder': str(folder), 'size': out.stat().st_size, 'mime': 'audio/mp4',
        'meta': {
            'title': info.get('title') or '', 'uploader': info.get('uploader') or info.get('channel') or '',
            'duration': info.get('duration'), 'thumbnail': info.get('thumbnail'), 'ext': 'm4a',
        },
    }


def forget_stream(stream):
    if stream.get('folder'):
        shutil.rmtree(stream['folder'], ignore_errors=True)  # файл ещё отдаётся — удалится при следующем запуске


def find_stream(url, fresh=False):
    """Ссылка на поток из кэша или новый поиск через yt-dlp (без скачивания)."""
    with streams_lock:
        lock = stream_locks.setdefault(url, threading.Lock())
    with lock:
        now = time.time()
        with streams_lock:
            for key in [k for k, v in streams.items() if v['expires'] < now]:
                forget_stream(streams.pop(key))
            for key in [k for k, v in stream_locks.items() if k not in streams and k != url and not v.locked()]:
                del stream_locks[key]  # блокировки старых ссылок, иначе словарь только растёт
            cached = streams.get(url)
        if cached and not (fresh and 'error' not in cached):
            if 'error' in cached:
                raise StreamError(cached['error'])
            cached['expires'] = now + STREAM_TTL
            return cached
        try:
            found = resolve_stream(url)
        except StreamError as e:
            with streams_lock:
                streams[url] = {'error': str(e), 'expires': now + STREAM_ERROR_TTL}
            raise
        found['expires'] = time.time() + STREAM_TTL
        with streams_lock:
            streams[url] = found
        return found


class NoCookieRedirect(urllib.request.HTTPRedirectHandler):
    """urllib при переадресации переносит все заголовки, в том числе Cookie, — на другой сайт не отдаём."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).hostname != urllib.parse.urlsplit(req.full_url).hostname:
            new.remove_header('Cookie')
        return new


# только http(s): file:// и ftp:// с чужой страницы через нас читать нельзя
upstream_opener = urllib.request.build_opener(NoCookieRedirect)


def open_upstream(stream, start, end):
    if urllib.parse.urlsplit(stream['src']).scheme not in ('http', 'https'):
        raise urllib.error.URLError('поток не по http(s)')
    headers = {**stream['headers'], 'Range': f'bytes={start}-{end}'}
    if stream.get('proxy'):
        # ссылка на поток привязана к адресу, с которого её нашли, — качаем через тот же прокси,
        # сетью yt-dlp (она умеет и socks). Куки он подставит сам и только своему сайту.
        headers.pop('Cookie', None)
        if 'ydl' not in stream:
            stream['ydl'] = yt_dlp.YoutubeDL({**base_opts(), 'proxy': stream['proxy'], 'socket_timeout': 20})
        try:
            return stream['ydl'].urlopen(yt_dlp.networking.Request(stream['src'], headers=headers))
        except yt_dlp.networking.exceptions.HTTPError as e:
            raise urllib.error.HTTPError(stream['src'], e.status, e.reason, e.response.headers, None) from None
        except yt_dlp.networking.exceptions.RequestError as e:
            raise urllib.error.URLError(str(e)) from None
    req = urllib.request.Request(stream['src'], headers=headers)
    return upstream_opener.open(req, timeout=20)


@app.post('/api/listen')
def api_listen():
    """Готовит прослушивание: ищет поток и отвечает понятной ошибкой, если послушать нельзя."""
    url = text_arg(json_body(), 'url')
    if not URL_RE.match(url):
        return jsonify(error=tr('Нужна ссылка на песню или видео.', 'A link to a song or video is required.')), 400
    try:
        return jsonify(ok=True, **find_stream(url)['meta'])
    except StreamError as e:
        return jsonify(error=str(e)), 400


@app.get('/api/stream')
def api_stream():
    """Звук песни для <audio>: с поддержкой Range, чтобы работала перемотка."""
    url = request.args.get('url', '').strip()
    if not URL_RE.match(url):
        return jsonify(error=tr('Нужна ссылка на песню или видео.', 'A link to a song or video is required.')), 400
    m = re.fullmatch(r'bytes=(\d*)-(\d*)', request.headers.get('Range', '').strip())
    start = int(m.group(1)) if m and m.group(1) else 0
    end = int(m.group(2)) if m and m.group(2) else None
    for attempt in (0, 1):
        try:
            stream = find_stream(url, fresh=attempt == 1)
        except StreamError as e:
            return jsonify(error=str(e)), 400
        if stream.get('file'):  # скачанная копия: Range и 416 send_file делает сам
            try:
                resp = send_file(stream['file'], mimetype=stream['mime'], conditional=True, max_age=0)
            except FileNotFoundError:
                return jsonify(error=tr('Файл для прослушивания пропал — включите песню ещё раз.', 'The listening file is gone — play the song again.')), 410
            resp.headers['Cache-Control'] = 'no-store'
            return resp
        if stream['size'] and start >= stream['size']:
            return Response(status=416, headers={'Content-Range': f"bytes */{stream['size']}"})
        try:
            first = open_upstream(stream, start, start + STREAM_CHUNK - 1 if end is None
                                  else min(end, start + STREAM_CHUNK - 1))
            break
        except urllib.error.HTTPError as e:
            if e.code == 416:
                return Response(status=416)
            if attempt:  # ссылка протухла, а новая тоже не открылась
                return jsonify(error=human_error(e)), 502
        except OSError as e:
            return jsonify(error=tr('Сайт не отвечает: ', 'The site does not respond: ') + str(e)), 502

    headers = {'Content-Type': stream['mime'], 'Cache-Control': 'no-store'}
    if first.status != 206:  # сайт не умеет Range — отдаём как есть, без перемотки
        if first.headers.get('Content-Length'):
            headers['Content-Length'] = first.headers['Content-Length']
        return Response(iter(lambda: first.read(64 << 10), b''), headers=headers, direct_passthrough=True)
    try:
        total = int(first.headers.get('Content-Range', '').rsplit('/', 1)[1])
    except (IndexError, ValueError):  # «bytes 0-99/*» — размер неизвестен
        return Response(iter(lambda: first.read(64 << 10), b''), headers=headers, direct_passthrough=True)
    stream['size'] = total
    end = total - 1 if end is None else min(end, total - 1)

    def body():
        resp, pos = first, start
        try:
            while True:
                while chunk := resp.read(64 << 10):
                    pos += len(chunk)
                    yield chunk
                resp.close()
                if pos > end:
                    return
                resp = open_upstream(stream, pos, min(end, pos + STREAM_CHUNK - 1))
        finally:
            resp.close()

    headers.update({'Accept-Ranges': 'bytes', 'Content-Length': str(end - start + 1)})
    if m:
        headers['Content-Range'] = f'bytes {start}-{end}/{total}'
    return Response(body(), status=206 if m else 200, headers=headers, direct_passthrough=True)


def check_download(data):
    """Общая проверка вариант + папка. Возвращает (folder, ошибка)."""
    option = text_arg(data, 'option')
    if not (re.fullmatch(r'v(\d{1,4}|best)', option) or option in ('a_mp3', 'a_m4a')):
        return None, tr('Неизвестный вариант.', 'Unknown option.')
    track = text_arg(data, 'track')
    langs = data.get('langs') if isinstance(data.get('langs'), list) else []
    if track and track != 'all' and not LANG_ID.fullmatch(track) or len(langs) > 60 or             not all(isinstance(x, str) and LANG_ID.fullmatch(x) for x in langs):
        return None, tr('Неизвестная звуковая дорожка.', 'Unknown audio track.')
    if data.get('subs') is not None and not parse_subs(data):
        return None, tr('Неизвестные субтитры.', 'Unknown subtitles.')
    folder = Path(text_arg(data, 'dir')) if text_arg(data, 'dir') else download_dir()
    if not folder.is_absolute() or not folder.is_dir():
        return None, tr('Папка не найдена: ', 'Folder not found: ') + str(folder)
    return folder, None


def parse_subs(data):
    """{'lang': 'ru', 'auto': false, 'embed': true} → такой же словарь или None."""
    subs = data.get('subs')
    if not isinstance(subs, dict) or not isinstance(subs.get('lang'), str) or not SUB_ID.fullmatch(subs['lang']):
        return None
    return {'lang': subs['lang'], 'auto': subs.get('auto') is True, 'embed': subs.get('embed') is True}


def parse_section(data):
    """Начало и конец фрагмента в секундах → (start, end), None или текст ошибки."""
    start, end = data.get('start'), data.get('end')
    if start in (None, '') and end in (None, ''):
        return None
    try:
        start = float(start or 0)
        end = float(end) if end not in (None, '') else float(data.get('duration') or 0)
    except (TypeError, ValueError):
        return tr('Время фрагмента указано неверно.', 'The clip time is invalid.')
    if not (math.isfinite(start) and math.isfinite(end)):
        return tr('Время фрагмента указано неверно.', 'The clip time is invalid.')
    if not end:
        return tr('Укажите, где фрагмент заканчивается.', 'Specify where the clip ends.')
    if start < 0 or end <= start:
        return tr('Конец фрагмента должен быть позже начала.', 'The clip end must be after its start.')
    return start, end


def new_task(data, folder, url, title, seq, index=0, section=None, from_list=False):
    option = data['option']
    label = text_arg(data, 'label')[:100]
    if section:
        label += f' · {clock(section[0])}–{clock(section[1])}'
    task = {
        'id': uuid.uuid4().hex[:10],
        'url': url, 'title': title, 'label': label,
        'option': option,
        'kind': 'video' if option.startswith('v') else 'audio',
        'origin': 'music' if data.get('origin') == 'music' else 'link',  # вкладка, где начали: там и показываем
        'playlist': bool(data.get('playlist')) and not from_list,
        'count': data.get('count') if isinstance(data.get('count'), int) else 0,
        'dir': str(folder),
        'section': section, 'from_list': from_list,
        'track': text_arg(data, 'track'), 'langs': data['langs'] if isinstance(data.get('langs'), list) else [],
        'subs': parse_subs(data) if option.startswith('v') and not section and not from_list else None,
        'loop': data.get('loop') is not False,  # COUB: видео по кругу под звук, если не выбрали иначе
        'status': 'running', 'stage': tr('Подготовка', 'Preparing'), 'percent': 0,
        'speed': '', 'eta': '', 'item': '', 'error': '', 'error_detail': '',
        'files': [], 'tmp': set(), 'procs': [], 'cancel': False,
        'sort': (-seq, index),  # новые запросы сверху, ссылки из одного списка — по порядку
    }
    with tasks_lock:
        tasks[task['id']] = task
    return task


@app.post('/api/download')
def api_download():
    data = json_body()
    folder, error = check_download(data)
    if error:
        return jsonify(error=error), 400
    section = parse_section(data)
    if isinstance(section, str):
        return jsonify(error=section), 400
    url = text_arg(data, 'url')
    if not URL_RE.match(url):
        return jsonify(error=tr('Нужна ссылка на видео.', 'A video link is required.')), 400
    task = new_task(data, folder, url, text_arg(data, 'title') or url, next(request_seq), section=section)
    threading.Thread(target=run_task, args=(task,), daemon=True).start()
    return jsonify(id=task['id'])


@app.post('/api/batch')
def api_batch():
    """Несколько ссылок с одним вариантом: ставим в очередь, качаются по одной."""
    data = json_body()
    folder, error = check_download(data)
    if error:
        return jsonify(error=error), 400
    # titles — необязательно, по порядку ссылок (песни из поиска «Музыки» — названия уже известны)
    titles = data.get('titles') if isinstance(data.get('titles'), list) else []
    urls = {}
    for i, u in enumerate(data.get('urls') or []):
        if isinstance(u, str) and URL_RE.match(u.strip()):
            title = titles[i] if i < len(titles) and isinstance(titles[i], str) else ''
            urls.setdefault(u.strip(), title.strip()[:300] or u.strip())
    if not urls:
        return jsonify(error=tr('Не нашёл ни одной ссылки.', 'No links found.')), 400
    seq = next(request_seq)
    ids = []
    for i, (url, title) in enumerate(urls.items()):
        task = new_task(data, folder, url, title, seq, i, from_list=True)
        task['status'], task['stage'] = 'queued', tr('В очереди', 'Queued')
        download_queue.put(task)
        ids.append(task['id'])
    return jsonify(ids=ids)


def queue_worker():
    while True:
        task = download_queue.get()
        with tasks_lock:
            if task['status'] != 'queued':  # отменили, пока ждала очереди
                continue
            task['status'], task['stage'] = 'running', tr('Подготовка', 'Preparing')
        try:
            run_task(task)
        except Exception as e:  # noqa: BLE001 — иначе поток очереди умрёт и остальные ссылки зависнут
            task['status'], task['stage'], task['error'] = 'error', tr('Ошибка', 'Error'), str(e)


threading.Thread(target=queue_worker, daemon=True).start()


# ---------- «Сжать» скачанное видео: H.265 на встроенном ffmpeg, звук без изменений ----------
VIDEO_EXTS = {'.mp4', '.mkv', '.webm', '.mov', '.m4v', '.avi', '.flv', '.ts'}
COMPRESS_VIDEO = ['-c:v', 'libx265', '-preset', 'fast', '-crf', '26', '-tag:v', 'hvc1']  # hvc1 — иначе iPhone/QuickTime не играют
compress_queue = queue.Queue()  # сжатие грузит процессор целиком, поэтому по одному
FFMPEG = BIN / ('ffmpeg.exe' if os.name == 'nt' else 'ffmpeg')


def video_duration(path):
    """Длительность в секундах из «Duration:» в выводе ffmpeg (ffprobe в imageio-ffmpeg нет)."""
    out = subprocess.run([str(FFMPEG), '-hide_banner', '-nostdin', '-i', path],
                         capture_output=True, text=True, encoding='utf-8', errors='replace')
    m = re.search(r'Duration: (\d+):(\d+):(\d+(?:\.\d+)?)', out.stderr)
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0


def free_name(path):
    """«Имя (сжато).mp4», а если такой уже есть — «Имя (сжато 2).mp4»."""
    target = path.with_name(f'{path.stem} (сжато).mp4')
    n = 2
    while target.exists():
        target = path.with_name(f'{path.stem} (сжато {n}).mp4')
        n += 1
    return target


def size_mb(num):
    mb = tr('МБ', 'MB')
    return f'{num / 1024 / 1024:.0f} {mb}' if num >= 10 * 1024 * 1024 else f'{num / 1024 / 1024:.1f} {mb}'


def compress_file(task, src):
    """Сжимает один файл. Возвращает путь к сжатому или None, если меньше не вышло."""
    duration = video_duration(src)
    target = free_name(Path(src))
    part = target.with_name(target.name + '.part')
    task['tmp'].add(str(part))
    priority = subprocess.BELOW_NORMAL_PRIORITY_CLASS if os.name == 'nt' else 0  # компьютер не «тормозит»
    for audio in (['-c:a', 'copy'], ['-c:a', 'aac', '-b:a', '192k']):  # не всякий звук влезает в MP4 как есть
        cmd = [str(FFMPEG), '-hide_banner', '-nostdin', '-y', '-i', src, '-map', '0:v:0', '-map', '0:a?',
               *COMPRESS_VIDEO, *audio, '-movflags', '+faststart', '-f', 'mp4',
               '-progress', 'pipe:1', '-nostats', '-loglevel', 'error', str(part)]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding='utf-8', errors='replace', creationflags=priority)
        task['procs'].append(proc)
        if task['cancel']:
            proc.kill()
        errors = []
        reader = threading.Thread(target=lambda p=proc: errors.extend(p.stderr), daemon=True)
        reader.start()
        speed = 0
        for line in proc.stdout:
            key, _, value = line.strip().partition('=')
            if key == 'speed' and value.endswith('x'):
                try:
                    speed = float(value[:-1])
                except ValueError:
                    pass
            elif key == 'out_time_us' and value.isdigit() and duration:
                done = int(value) / 1e6
                task['percent'] = min(99.9, round(done / duration * 100, 1))
                task['eta'] = clock((duration - done) / speed) if speed > 0 else ''
        proc.wait()
        reader.join(5)
        task['procs'].remove(proc)
        if task['cancel']:
            raise Cancelled()
        if proc.returncode == 0:
            break
    else:
        raise RuntimeError('ffmpeg не смог сжать видео: ' + ''.join(errors).strip()[-500:])
    before, after = os.path.getsize(src), part.stat().st_size
    if after > before * 0.95:  # уже хорошо сжато — второй файл почти того же размера не нужен
        part.unlink()
        return None, before, after
    part.rename(target)
    return str(target), before, after


def run_compress(task):
    total_before = total_after = 0
    kept = 0
    try:
        for i, src in enumerate(task['sources'], 1):
            if len(task['sources']) > 1:
                task['item'] = tr('{} из {}', '{} of {}').format(i, len(task['sources']))
            task['percent'] = 0
            path, before, after = compress_file(task, src)
            if path:
                task['files'].append(path)
                total_before, total_after = total_before + before, total_after + after
            else:
                kept += 1
        task['status'], task['stage'], task['percent'] = 'done', tr('Готово', 'Done'), 100
        if task['files']:
            saved = round((1 - total_after / total_before) * 100)
            task['label'] = tr('Сжато: {} → {} (меньше на {}%)', 'Compressed: {} → {} ({}% smaller)').format(
                size_mb(total_before), size_mb(total_after), saved)
            if kept:
                task['label'] += tr(' · {} уже были сжаты хорошо', ' · {} were already well compressed').format(kept)
        else:
            task['label'] = tr('Видео уже сжато хорошо — меньше не получилось, оставил как было',
                               'The video is already well compressed — could not make it smaller, kept it as is')
    except Cancelled:
        task['status'], task['stage'] = 'cancelled', tr('Отменено', 'Cancelled')
    except Exception as e:  # noqa: BLE001
        if task['cancel']:
            task['status'], task['stage'] = 'cancelled', tr('Отменено', 'Cancelled')
        else:
            task['status'], task['stage'] = 'error', tr('Ошибка', 'Error')
            task['error'], task['error_detail'] = tr('Не удалось сжать видео.', 'Could not compress the video.'), str(e)
    task['item'] = task['eta'] = ''
    for name in task['tmp']:  # недоделанный .part после отмены или ошибки
        if name.endswith('.part'):
            try:
                os.remove(name)
            except OSError:
                pass


def compress_worker():
    while True:
        task = compress_queue.get()
        with tasks_lock:
            if task['status'] != 'queued':
                continue
            task['status'], task['stage'] = 'running', tr('Сжимаю', 'Compressing')
        run_compress(task)


threading.Thread(target=compress_worker, daemon=True).start()


@app.post('/api/tasks/<task_id>/compress')
def api_compress(task_id):
    source = tasks.get(task_id)
    files = [f for f in (source or {}).get('files', [])
             if Path(f).suffix.lower() in VIDEO_EXTS and os.path.exists(f)]
    if not source or source['status'] != 'done' or not files:
        return jsonify(error=tr('Сжать можно только скачанное видео, которое лежит на месте.',
                                 'Only a downloaded video that is still in place can be compressed.')), 400
    task = new_task({'option': 'compress'}, Path(files[0]).parent, '', source['title'], next(request_seq))
    task.update(kind='compress', label=tr('Сжатие видео', 'Video compression'), status='queued', stage=tr('В очереди', 'Queued'),
                source=task_id, sources=files, origin=source.get('origin', 'link'))
    compress_queue.put(task)
    return jsonify(id=task['id'])


@app.get('/api/tasks')
def api_tasks():
    with tasks_lock:
        # files копируем: поток загрузки дописывает его, пока ответ превращается в JSON
        items = [{k: list(v) if k == 'files' else v for k, v in t.items() if k not in ('cancel', 'tmp', 'procs', 'sort')}
                 for t in sorted(tasks.values(), key=lambda t: t['sort'])]
    return jsonify(items)


def cancel_task(task):
    with tasks_lock:
        if task['status'] == 'queued':
            task['status'], task['stage'] = 'cancelled', tr('Отменено', 'Cancelled')
            return
    task['cancel'] = True
    for proc in list(task['procs']):
        if proc.poll() is None:
            proc.kill()


@app.post('/api/tasks/<task_id>/cancel')
def api_cancel(task_id):
    task = tasks.get(task_id)
    if task:
        cancel_task(task)
    return jsonify(ok=True)


@app.post('/api/tasks/<task_id>/remove')
def api_remove(task_id):
    with tasks_lock:
        task = tasks.get(task_id)
        if task and task['status'] not in ('running', 'queued'):
            tasks.pop(task_id)
    return jsonify(ok=True)


@app.post('/api/tasks/clear')
def api_clear():
    """Убирает из списка всё, что уже не качается и не ждёт очереди (ids — только эти: список одной вкладки)."""
    ids = json_body().get('ids')
    with tasks_lock:
        for task_id in [k for k, t in tasks.items() if t['status'] not in ('running', 'queued')
                        and (not isinstance(ids, list) or k in ids)]:
            tasks.pop(task_id)
    return jsonify(ok=True)


@app.post('/api/open')
def api_open():
    task = tasks.get(text_arg(json_body(), 'id'))
    files = [f for f in (task or {}).get('files', []) if os.path.exists(f)]
    if os.name != 'nt':
        return jsonify(ok=False)
    if files:
        show_in_explorer(files[-1], select=True)
    else:
        open_downloads()
    return jsonify(ok=True)


@app.get('/api/settings')
def api_settings():
    s = load_settings()
    return jsonify(download_dir=str(download_dir()), ask_every_time=s['ask_every_time'],
                   proxy=s['proxy'], proxy_all=s['proxy_all'], proxy_sites=s['proxy_sites'], doh=s['doh'],
                   lang=i18n.CURRENT)


@app.post('/api/settings')
def api_settings_save():
    data = json_body()
    if 'ask_every_time' in data:
        save_settings(ask_every_time=bool(data['ask_every_time']))
    if 'proxy_all' in data:
        save_settings(proxy_all=bool(data['proxy_all']))
    if 'doh' in data:
        save_settings(doh=bool(data['doh']))
    if data.get('lang') in i18n.LANGS:
        save_settings(lang=data['lang'])
        i18n.set_lang(data['lang'])
        if tray_icon:
            tray_icon.title = APP_NAME()
            tray_icon.update_menu()
    if 'proxy' in data:
        proxy = normalize_proxy(text_arg(data, 'proxy')) if text_arg(data, 'proxy') else ''
        if proxy is None:
            return jsonify(error=tr('Это не похоже на адрес прокси. Пример: ', 'This does not look like a proxy address. Example: ')
                           + 'socks5://127.0.0.1:10808'), 400
        save_settings(proxy=proxy)
    return api_settings()


@app.post('/api/proxy/check')
def api_proxy_check():
    """С адресом — проверяет этот прокси, без адреса — ищет прокси VPN-программы на компьютере."""
    text = text_arg(json_body(), 'proxy')
    if text:
        proxy = normalize_proxy(text)
        if proxy is None:
            return jsonify(error=tr('Это не похоже на адрес прокси. Пример: ', 'This does not look like a proxy address. Example: ')
                           + 'socks5://127.0.0.1:10808'), 400
        if not proxy_alive(proxy):
            return jsonify(ok=False, error=tr('По этому адресу никто не отвечает. Включена ли VPN-программа?',
                                        'Nothing answers at this address. Is the VPN app running?'))
        if not proxy_works(proxy):
            return jsonify(ok=False, error=tr('Прокси отвечает, но YouTube через него не открылся.',
                                        'The proxy answers, but YouTube did not open through it.'))
    else:
        proxy = find_proxy()
        if not proxy:
            return jsonify(ok=False, error=tr('Прокси VPN-программы на компьютере не найден. Включите VPN-программу '
                                              'или впишите адрес из её настроек (раздел «Прокси», «SOCKS», «Порт»).',
                                              'No VPN app proxy found on this computer. Turn on the VPN app '
                                              'or enter the address from its settings (the “Proxy”, “SOCKS” or “Port” section).'))
    save_settings(proxy=proxy)
    return jsonify(ok=True, proxy=proxy)


# ---------- открытые вкладки программы ----------
# Каждая открытая страница держит поток событий /api/events. Повторный запуск (ярлык, «Открыть» у часов)
# спрашивает страницы «есть кто?» и, если какая-то отозвалась, новую вкладку не открывает.
pages = set()                     # очереди сообщений для открытых страниц
pages_lock = threading.Lock()
page_answered = threading.Event()
page_opening = threading.Event()  # браузеру уже велели открыть страницу (заставка ждёт её подключения)
tray_icon = None


@app.get('/api/events')
def api_events():
    q = queue.Queue()
    if SPLASH and page_opening.is_set():  # открытая нами вкладка подключилась — заставка больше не нужна
        SPLASH.close()
    with pages_lock:
        pages.add(q)

    def stream():
        try:
            yield 'retry: 2000\n\n'
            while True:
                try:
                    yield f'data: {q.get(timeout=15)}\n\n'
                except queue.Empty:
                    yield ': ping\n\n'  # так и узнаём, что вкладку закрыли: запись не пройдёт
        finally:
            with pages_lock:
                pages.discard(q)

    return Response(stream(), mimetype='text/event-stream', headers={'Cache-Control': 'no-store'})


@app.post('/api/here')
def api_here():
    page_answered.set()
    return jsonify(ok=True)


def tell_pages(message):
    with pages_lock:
        for q in pages:
            q.put(message)
        return bool(pages)


def notify(text):
    """Всплывающее сообщение у значка возле часов (без значка — в консоль)."""
    print(text)
    if tray_icon is not None:
        try:
            tray_icon.notify(text, APP_NAME())
        except Exception:  # noqa: BLE001
            pass


def show_page(quiet=False):
    """Открывает страницу программы, только если её нет ни в одной вкладке. Если есть — вкладка мигает
    заголовком, а у часов всплывает подсказка (quiet — не всплывает: о том же пишет заставка).
    True — открыли новую вкладку."""
    page_answered.clear()
    if tell_pages('ping') and page_answered.wait(2):
        tell_pages('here')
        if SPLASH and SPLASH.alive:
            SPLASH.close(str(ALREADY_OPEN_TEXT), 2.5)
        elif not quiet:
            notify(tr('Программа уже открыта — во вкладке браузера «Универсальный скачиватель».',
                       'The program is already open — in the “Universal Downloader” browser tab.'))
        return False
    if SPLASH and SPLASH.alive:
        SPLASH.set_text(tr('Открываю страницу в браузере…', 'Opening the page in the browser…'))
        page_opening.set()
        threading.Thread(target=close_splash_later, daemon=True).start()
    webbrowser.open(f'http://{HOST}:{PORT}')
    return True


ALREADY_OPEN_TEXT = Text('Уже открыта — во вкладке браузера', 'Already open — in a browser tab')


def close_splash_later():
    """Браузер может запускаться долго, но вечно заставку не держим. Страница так и не подключилась —
    говорим у часов, где программа."""
    time.sleep(20)
    if SPLASH.alive:
        SPLASH.close()
        notify(tr('Программа запущена: значок — здесь, у часов, страница — ',
                   'The program is running: the icon is here by the clock, the page is ') + f'http://{HOST}:{PORT}')


@app.post('/api/show')
def api_show():
    """Программу запустили ещё раз (второй процесс) — показываем страницу.
    quiet — у второго процесса своя заставка, подсказка у часов не нужна."""
    return jsonify(opened=show_page(quiet=bool(json_body().get('quiet'))))


@app.post('/api/folder/pick')
def api_pick_folder():
    """Открывает окно Windows «Выбор папки». Выбранная папка запоминается."""
    path = pick_folder(download_dir())
    if not path:
        return jsonify(cancelled=True)
    save_settings(download_dir=path)
    return jsonify(cancelled=False, download_dir=path)


def port_owner():
    """None — порт свободен, 'ours' — на нём уже наша программа, 'other' — чужая."""
    with socket.socket() as s:
        if s.connect_ex((HOST, PORT)) != 0:
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # системный прокси тут ни к чему
    try:
        with opener.open(f'http://{HOST}:{PORT}/api/status', timeout=15) as r:
            return 'ours' if 'ytdlp' in json.loads(r.read()) else 'other'
    except Exception:  # noqa: BLE001
        return 'other'


def fail(message):
    """Без окна print никто не увидит — показываем окно с ошибкой."""
    print(message)
    if SPLASH:
        SPLASH.close()
    if TRAY and os.name == 'nt':
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, APP_NAME(), 0x10)


BUSY_TEXT = Text('Порт {port} занят другой программой, поэтому скачиватель не запустился. '
                 'Поменяйте «port» в settings.json (например, на 8766) и запустите снова.',
                 'Port {port} is used by another program, so the downloader did not start. '
                 'Change “port” in settings.json (for example, to 8766) and start again.')


def open_downloads():
    folder = download_dir()
    folder.mkdir(exist_ok=True)
    show_in_explorer(folder)


def quit_app(icon):
    """«Выход» из меню у часов: отменяем загрузки, убираем недокачанное и закрываемся."""
    running = [t for t in list(tasks.values()) if t['status'] in ('running', 'queued')]
    for task in running:
        cancel_task(task)
    deadline = time.time() + 10
    while time.time() < deadline and any(t['status'] == 'running' for t in running):
        time.sleep(0.2)
    icon.stop()


def update_ytdlp_daily():
    """Без окна Запуск.bat не обновляет yt-dlp, поэтому раз в сутки делаем это тихо сами.
    Новая версия заработает со следующего запуска."""
    stamp = Path(sys.prefix) / 'ytdlp-updated'  # папка python (встроенный) или .venv
    if stamp.exists() and time.time() - stamp.stat().st_mtime < 24 * 3600:
        return
    out = subprocess.run(
        [sys.executable, '-m', 'pip', 'install', '-q', '-U', '--disable-pip-version-check', 'yt-dlp[default,curl-cffi]'],
        capture_output=True, text=True)
    if out.returncode == 0:
        stamp.touch()
    else:
        print('Не удалось обновить yt-dlp:', out.stderr.strip()[-500:])


def register_protocol():
    """Ссылка udl-downloader: запускает программу без новой вкладки — так её включает кнопка расширения
    в браузере. Пишем в реестр пользователя (HKCU) при каждом запуске: папку программы могли перенести.
    Тестовый запуск (DOWNLOADER_PORT) реестр не трогает — иначе ссылка вела бы во временную копию."""
    if os.name != 'nt' or os.environ.get('DOWNLOADER_PORT'):
        return
    import winreg
    wscript = Path(os.environ.get('SystemRoot', 'C:\\Windows')) / 'System32' / 'wscript.exe'
    root = rf'Software\Classes\{PROTOCOL}'
    values = {
        root: {'': 'URL:Универсальный скачиватель', 'URL Protocol': ''},
        root + r'\DefaultIcon': {'': str(ICON)},
        root + r'\shell\open': {'FriendlyAppName': 'Универсальный скачиватель'},
        root + r'\shell\open\command': {'': f'"{wscript}" "{BASE / "launch.vbs"}" --no-browser'},
    }
    try:
        for key, items in values.items():
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
                for name, value in items.items():
                    winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
    except OSError as e:
        print('Не удалось прописать ссылку udl-downloader:', e)


def open_page_later():
    """Первый запуск: ждём пару секунд — вкладка, оставшаяся с прошлого раза, сама переподключится,
    и тогда новую не открываем."""
    deadline = time.time() + 2.5
    while time.time() < deadline and not pages:
        time.sleep(0.1)
    show_page()


def run_tray():
    global tray_icon
    import pystray
    from PIL import Image

    def open_page():  # в своём потоке: ответа страниц ждём до 2 с, а меню у часов не должно зависать
        threading.Thread(target=show_page, daemon=True).start()

    menu = pystray.Menu(
        pystray.MenuItem(lambda item: tr('Открыть', 'Open'), open_page, default=True),  # и по щелчку на значке
        pystray.MenuItem(lambda item: tr('Папка загрузок', 'Downloads folder'), open_downloads),
        pystray.MenuItem(lambda item: tr('Обновить до версии ', 'Update to version ') + app_update['latest']['version'] if app_update['latest'] else '',
                         open_page, visible=lambda item: bool(app_update['latest']) and not DEV_COPY),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lambda item: tr('Выход', 'Exit'), quit_app),
    )

    def setup(icon):
        icon.visible = True
        result = app_update['result']
        if result:
            notify(tr('Программа обновлена до версии {}.', 'The program was updated to version {}.').format(result['to'])
                   if result.get('ok') else tr('Обновление не установилось: ', 'The update did not install: ')
                   + str(result.get('error')))

    tray_icon = pystray.Icon('downloader', Image.open(ICON), APP_NAME(), menu)
    tray_icon.run(setup)


def main():
    url = f'http://{HOST}:{PORT}'
    owner = port_owner()
    if owner == 'ours':  # уже запущено — открываем страницу, только если её нет ни в одной вкладке
        if '--no-browser' not in sys.argv:
            if SPLASH:
                SPLASH.set_text(tr('Уже запущена — открываю страницу…', 'Already running — opening the page…'))
            body = json.dumps({'quiet': bool(SPLASH)}).encode()
            req = urllib.request.Request(f'{url}/api/show', body, {'Content-Type': 'application/json'})
            try:
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=10) as r:
                    opened = json.loads(r.read()).get('opened')
            except (OSError, ValueError):
                opened = True
                webbrowser.open(url)
            if SPLASH:  # процесс сейчас завершится — ждём, пока надпись прочитают и заставка исчезнет
                SPLASH.close(None if opened else str(ALREADY_OPEN_TEXT), 1.0 if opened else 2.5)
                SPLASH.wait()
        elif SPLASH:
            SPLASH.close()
            SPLASH.wait()
        return
    if owner == 'other':
        fail(str(BUSY_TEXT).format(port=PORT))
        return
    prepare_ffmpeg()
    shutil.rmtree(LISTEN_DIR, ignore_errors=True)  # песни для прослушивания с прошлого раза
    register_protocol()
    load_app_update()
    DOWNLOADS.mkdir(exist_ok=True)
    if TRAY:
        logging.getLogger('werkzeug').setLevel(logging.ERROR)  # иначе каждый запрос страницы пишется в лог
    # порт занимаем до открытия браузера; app.run при ошибке завершает только свой поток (без окна — молча)
    from werkzeug.serving import make_server
    try:
        server = make_server(HOST, PORT, app, threaded=True)
    except (OSError, SystemExit):
        fail(str(BUSY_TEXT).format(port=PORT))
        return
    if '--no-browser' not in sys.argv:
        threading.Thread(target=open_page_later, daemon=True).start()
    elif SPLASH:  # запустила кнопка расширения — его вкладка сама перейдёт на страницу, как только порт ответит
        SPLASH.close(tr('Запущена', 'Running'), 1.0)
    if not DEV_COPY:
        threading.Thread(target=check_app_update_regularly, daemon=True).start()
    if not TRAY:
        print(f'Универсальный скачиватель запущен: {url}')
        print('Не закрывайте это окно, пока скачиваете. Закрыть — Ctrl+C.')
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        return
    # Без окна: сервер в фоне, а программа живёт значком у часов, пока не нажмут «Выход»
    print(f'Запущено: {url}')
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=update_ytdlp_daily, daemon=True).start()
    run_tray()


if __name__ == '__main__':
    main()
