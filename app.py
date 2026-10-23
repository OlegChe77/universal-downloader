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
if TRAY:  # окна нет — всё, что программа пишет (и ошибки запуска), сохраняем в файл
    sys.stdout = sys.stderr = open(BASE / 'program.log', 'w', encoding='utf-8', buffering=1)

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
DEFAULT_PORT = 8765
VERSION = '1.2'  # номер этой версии = тег релиза на GitHub без «v» (updater.py сверяет их)
UPDATE_API = (os.environ.get('DOWNLOADER_UPDATE_API')  # для проверки обновления без настоящего релиза
              or 'https://api.github.com/repos/OlegChe77/universal-downloader/releases/latest')
UPDATE_DIR = BASE / 'update'  # скачанная новая версия и старые файлы на время замены
UPDATE_CACHE = Path(sys.prefix) / 'app-update.json'  # когда проверяли и что нашли
UPDATE_EVERY = 3 * 24 * 3600
DEV_COPY = (BASE / '.git').exists()  # папка разработки обновляется через git, а не архивом
# proxy — прокси VPN-программы; proxy_all — все сайты через него; proxy_sites — сайты, которые не открылись напрямую
DEFAULT_SETTINGS = {'download_dir': str(DOWNLOADS), 'ask_every_time': True, 'port': DEFAULT_PORT,
                    'proxy': '', 'proxy_all': False, 'proxy_sites': []}
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


def settings_port():
    """Порт страницы: DOWNLOADER_PORT (для тестов) или «port» из settings.json."""
    try:
        port = int(os.environ.get('DOWNLOADER_PORT') or load_settings()['port'])
    except (TypeError, ValueError):
        return DEFAULT_PORT
    return port if 1024 <= port <= 65535 else DEFAULT_PORT


PORT = settings_port()
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
        return jsonify(error='Запрос с чужой страницы.'), 403
    if request.method == 'POST' and not request.is_json:
        return jsonify(error='Нужен JSON.'), 415
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
path = filedialog.askdirectory(parent=root, initialdir=sys.argv[1], title='Куда сохранить файлы?', mustexist=True)
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
$dialog.Description = 'Куда сохранить файлы?'
$dialog.SelectedPath = $env:UDL_PICK_INITIAL
if ($dialog.ShowDialog($owner) -eq 'OK') { $dialog.SelectedPath }
'''


def pick_folder(initial):
    env = {**os.environ, 'PYTHONIOENCODING': 'utf-8', 'UDL_PICK_INITIAL': str(initial)}
    if importlib.util.find_spec('tkinter'):
        cmd = [sys.executable, '-c', PICK_FOLDER_SCRIPT, str(initial)]
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


VPN_HINT = ' Если включён VPN — выключите его или смените сервер.'
COOKIES_HINT = ' Нужен cookies.txt из браузера, где вы вошли на сайт: положите его в папку программы (как — на странице «Помощь»).'
UPDATE_HINT = ' Обновите yt-dlp на странице «Помощь».'
BLOCK_HINT = (' Если сайт заблокирован (YouTube, Instagram, Pornhub…), нужен VPN для всего компьютера или его прокси: '
              'VPN-расширение в браузере на программу не действует. Как быть — «Помощь» → «Если включён VPN».')

# (регулярное выражение по тексту ошибки, объяснение). Порядок важен: сначала узкие случаи.
# yt-dlp дописывает к разным ошибкам «Use --cookies…», поэтому «вход в аккаунт» стоит после
# «не бот», «18+» и платного; 403/404 — по точным словам, а не по цифрам в адресе.
ERROR_RULES = [
    # yt-dlp сам заблокировал некоторые сайты (Suno и др.) из-за юридических рисков
    (r'\[liability\]|will not be supported',
     'Этот сайт yt-dlp не поддерживает намеренно (по юридическим причинам) — обновление не поможет. '
     'Скачайте файл на самом сайте, если он это позволяет.'),
    (r'\bdrm\b',
     'Видео защищено DRM (как в Netflix, Кинопоиске, Okko, Spotify) — скачать его нельзя.'),
    (r"not a bot|confirm you.?re not",
     'YouTube просит подтвердить, что вы не робот. Так бывает с VPN и с адресами, откуда качают много.'
     + VPN_HINT + COOKIES_HINT),
    (r'confirm your age|age[- ]restricted|inappropriate for some users|18\+|age verification|must be 18|'
     r'not be comfortable for some audiences',
     'Видео с возрастным ограничением (18+): сайт отдаёт его только после входа в аккаунт.' + COOKIES_HINT),
    (r'members[- ]only|join this channel|\bpremium\b|requires? (a )?(payment|purchase|subscription)|\bpaid\b|\brental\b',
     'Видео платное или только для подписчиков — скачать можно, только если у вас есть доступ.' + COOKIES_HINT),
    (r'premieres? in|live event will begin|is upcoming',
     'Трансляция или премьера ещё не началась. Попробуйте, когда видео появится.'),
    (r'geo.?restrict|not available (in|from) your (country|location)|in your country|blocked it in your',
     'Видео недоступно в вашей стране — сайт ограничил показ по месту.'
     ' С зарубежным VPN так бывает у русских сайтов (Rutube, VK, ok.ru), без VPN — у зарубежных.'),
    (r'\bprivate\b',
     'Видео закрыто автором (приватное) — скачать можно, только если у вашего аккаунта есть доступ.' + COOKIES_HINT),
    # «This content isn't available, try again later… rate-limited» — это не удалённое видео, а ограничение
    (r'rate.?limit|http error 429|too many requests',
     'Сайт временно ограничил загрузки: слишком много запросов. Подождите 10–30 минут.' + VPN_HINT),
    # TikTok так отвечает (статус 10204) и на удалённый пост, и на закрытый для страны
    (r'ip address is blocked',
     'TikTok не отдаёт этот пост: его удалили или он закрыт для вашей страны. Проверьте ссылку в браузере; '
     'если открывается только с VPN — включите прокси во вкладке «VPN».'),
    # Instagram без входа показывает не всё: закрытые аккаунты, 18+, а иногда просто «много запросов»
    (r'empty media response',
     'Instagram не показал этот пост без входа в аккаунт: аккаунт закрытый, пост 18+ '
     'или Instagram ограничил просмотр без входа.' + COOKIES_HINT),
    (r'video (is )?(unavailable|not available)|has been removed|no longer available|'
     r'been deleted|does not exist|was deleted',
     'Видео удалено или недоступно. Проверьте ссылку в браузере.'),
    (r'unsupported url',
     'Этот сайт не поддерживается или по ссылке нет видео. Проверьте, что ссылка ведёт на страницу '
     'с самим видео, а не на ленту или главную.'),
    (r'no video in this post',
     'В этом посте нет видео — только фото. Программа качает видео и звук.'),
    (r'is not a valid url|no such file or directory: .?(<url>|https?)',
     'Это не похоже на ссылку. Скопируйте адрес из строки браузера целиком.'),
    (r'sign in|log ?in|cookies|authenticat|registered users',
     'Сайт требует вход в аккаунт.' + COOKIES_HINT),
    # разборщик сайта в yt-dlp не понял новую страницу (так было у ok.ru — исправлено в sites.py)
    (r'the json object must be|unable to extract|failed to parse|unexpected (response|data)|'
     r'please report this issue',
     'Сайт изменил страницу, и yt-dlp не смог её разобрать.' + UPDATE_HINT
     + ' Не поможет — подождите исправления, обычно его выпускают за несколько дней.'),
    (r'requested format is not available|no video formats found',
     'Такого качества у видео больше нет. Разберите ссылку заново и выберите другой вариант.'),
    # 3419392776 — так ffmpeg сообщает HTTP 404 (AVERROR_HTTP_NOT_FOUND)
    (r'http error 404|404: not found|code 3419392776',
     'Видео удалено или ссылка устарела (ошибка 404). Откройте его в браузере; если играет, '
     'нажмите правой кнопкой на самом видео → «Скачать в программе».'),
    # 410 — страницу убрали насовсем, но так же отвечают сайты, ушедшие из страны (PornHub и др.)
    (r'http error 410|410: gone',
     'Страница удалена или сайт закрыт для вашей страны: проверьте ссылку в браузере; '
     'если открывается только с VPN — включите прокси во вкладке «VPN».'),
    # 3436169992 — так ffmpeg сообщает HTTP 403 (AVERROR_HTTP_FORBIDDEN), бывает при загрузке куска
    (r'http error 403|forbidden|code 3436169992',
     'Сайт отказал в доступе (ошибка 403). Попробуйте ещё раз через минуту. Ссылки на видео привязаны '
     'к адресу — VPN нельзя включать или переключать во время загрузки. '
     'Повторяется — обновите yt-dlp на странице «Помощь».'),
    (r'http error 5\d\d|service unavailable|bad gateway|internal server error',
     'У сайта сбой на его стороне. Попробуйте позже.'),
    # так провайдер обрывает соединение с заблокированным сайтом
    (r'unexpected_eof|eof occurred in violation',
     'Сайт не открывается: соединение оборвали — похоже, он заблокирован.' + BLOCK_HINT),
    (r'certificate|\bssl\b|\btls\b',
     'Не удалось установить защищённое соединение с сайтом. Проверьте дату и время на компьютере; '
     'так бывает, когда антивирус проверяет HTTPS или VPN подменяет соединение.'),
    (r'getaddrinfo|name or service not known|failed to resolve|nodename nor servname|no address associated',
     'Не удалось найти сайт в сети. Проверьте интернет.' + BLOCK_HINT),
    (r'timed out|timeout|connection (reset|refused|aborted)|remote end closed|network is unreachable|'
     r'unable to connect|max retries|winerror 100(5\d|6\d)|incompleteread|connection broken|got only',
     'Сайт не отвечает или связь прервалась. Проверьте интернет и попробуйте ещё раз.' + BLOCK_HINT),
    (r'no space left|errno 28|winerror 112|not enough space',
     'На диске не хватает места. Освободите место или выберите другую папку для загрузок.'),
    (r'being used by another process|winerror 32',
     'Файл занят другой программой (плеером или антивирусом). Закройте его и попробуйте снова.'),
    (r'file name too long|errno 36|winerror 206|filename.*too long',
     'Слишком длинный путь к файлу. Выберите папку для загрузок поближе к корню диска, например D:\\Загрузки.'),
    (r'permission denied|errno 13|winerror 5\b|access is denied',
     'Нет доступа к папке загрузок. Выберите другую папку или проверьте, не блокирует ли её антивирус.'),
    (r'downloaded file is empty|did not get any data|empty file',
     'Сайт отдал пустой файл. Попробуйте ещё раз.' + VPN_HINT),
    (r'ffmpeg|postprocessing|conversion failed|merg',
     'Не удалось склеить или перевести файл (ffmpeg). Проверьте строку «ffmpeg» на странице «Помощь» '
     'или выберите другой вариант.'),
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
            return text
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
    for unit in ('Б', 'КБ', 'МБ', 'ГБ'):
        if num < 1024:
            return f'~{num:.1f} {unit}' if unit == 'ГБ' else f'~{num:.0f} {unit}'
        num /= 1024
    return f'~{num:.1f} ТБ'


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
        options.append({'id': 'vbest', 'kind': 'video', 'label': 'Видео', 'note': 'лучшее качество'})
    return options


AUDIO_OPTIONS = [
    {'id': 'a_mp3', 'kind': 'audio', 'label': 'MP3', 'note': 'лучшее качество'},
    {'id': 'a_m4a', 'kind': 'audio', 'label': 'M4A', 'note': 'без перекодирования'},
]


def clock(sec):
    """83.5 → '1:23', 3725 → '1:02:05'."""
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'


def download_opts(option, playlist, folder, section=None):
    """section — (начало, конец) в секундах, если нужен только кусок видео."""
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
        suffix = f' (фрагмент {clock(start)}-{clock(end)})'.replace(':', '.')
    if playlist:
        name = '%(playlist_title).80B/%(playlist_index)03d - %(title).120B.%(ext)s'
    else:
        name = f'%(title).150B [%(id)s]{suffix}.%(ext)s'
    opts['outtmpl'] = str(folder / name)

    if option.startswith('v'):
        h = option[1:]
        if h.isdigit():
            opts['format'] = f'bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b'
        else:
            opts['format'] = 'bv*+ba/b'
        # при одинаковом разрешении — H.264 + AAC: открывается любым плеером
        opts['format_sort'] = ['res', 'vcodec:h264', 'acodec:aac', 'ext:mp4:m4a']
        opts['merge_output_format'] = 'mp4'
    else:
        codec = option.split('_', 1)[1]
        opts['format'] = 'ba[ext=m4a]/ba/b' if codec == 'm4a' else 'ba/b'
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
            raise yt_dlp.utils.DownloadError(f'Имя файла выходит за папку загрузок: {path}')
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
            task['item'] = f"{info['playlist_index']} из {task['count']}"
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate')
            done = d.get('downloaded_bytes') or 0
            if total:
                task['percent'] = round(done / total * 100, 1)
            elif d.get('fragment_count'):
                task['percent'] = round((d.get('fragment_index') or 0) / d['fragment_count'] * 100, 1)
            is_audio = info.get('vcodec') == 'none'
            task['stage'] = 'Скачиваю звук' if is_audio and task['kind'] == 'video' else 'Скачиваю'
            task['speed'] = d.get('_speed_str', '').strip()
            task['eta'] = d.get('_eta_str', '').strip()
        elif d['status'] == 'finished':
            task['percent'] = 100

    def pp_progress(d):
        if task['cancel']:
            raise Cancelled()
        if d['status'] == 'started' and d.get('postprocessor') != 'SafeNames':  # проверка имён — не обработка
            task['stage'] = 'Обработка'
            task['speed'] = task['eta'] = ''

    def got_info(info, incomplete=False):
        if task['from_list'] and info.get('title') and not incomplete:
            task['title'] = info['title']  # в списке ссылок название узнаём только при загрузке
        if task['section']:
            task['stage'] = 'Скачиваю фрагмент'  # ffmpeg не сообщает проценты — полоса просто бежит
        return None

    task_of_thread[threading.get_ident()] = task
    try:
        opts = download_opts(task['option'], task['playlist'], Path(task['dir']), task['section'])
        opts['match_filter'] = got_info  # вызывается, когда сведения о видео уже получены
        opts['progress_hooks'] = [progress]
        opts['postprocessor_hooks'] = [pp_progress]
        opts['post_hooks'] = [lambda path: task['files'].append(path)]

        def work(proxy):
            with yt_dlp.YoutubeDL({**opts, 'proxy': proxy} if proxy else opts) as ydl:
                ydl.add_post_processor(SafeNamesPP(ydl, task['dir']), when='video')
                ydl.download([task['url']])

        via_proxy(task['url'], work)
        task['status'] = 'done'
        task['stage'] = 'Готово'
        task['percent'] = 100
    except Cancelled:
        task['status'] = 'cancelled'
        task['stage'] = 'Отменено'
    except Exception as e:  # noqa: BLE001 — показываем пользователю любую ошибку
        if task['cancel']:
            task['status'], task['stage'] = 'cancelled', 'Отменено'
        else:
            task['status'] = 'error'
            task['stage'] = 'Ошибка'
            task['error'] = human_error(e)
            task['error_detail'] = error_detail(e, task['error'])
    task['speed'] = task['eta'] = ''
    task_of_thread.pop(threading.get_ident(), None)
    task['procs'].clear()
    # чистим вне except: пока жив traceback, недокачанный файл остаётся открытым
    if task['status'] == 'cancelled':
        remove_partial(task)


@app.get('/')
def index():
    return send_from_directory(WEB, 'index.html')


@app.get('/help')
def help_page():
    return send_from_directory(WEB, 'help.html')


@app.get('/favicon.ico')
def favicon():
    return send_from_directory(WEB, 'icon.ico')


@app.get('/quick.js')
def quick_js():
    return send_from_directory(WEB, 'quick.js')


@app.get('/browsers.svg')
def browsers_svg():
    return send_from_directory(WEB, 'browsers.svg')


@app.get('/extension.zip')
def extension_zip():
    """Расширение для Chrome/Edge/Яндекса/Firefox. Файлы — в корне архива: «Извлечь всё» даёт папку
    сразу с manifest.json. В config.js подставляем порт, на котором работает программа."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for f in sorted(EXTENSION.rglob('*')):
            if f.is_file():
                name = f.relative_to(EXTENSION).as_posix()
                data = f'self.UDL_PORT = {PORT};\n'.encode() if name == 'config.js' else f.read_bytes()
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
        return jsonify(ok=False, error='\n'.join(tail) or 'pip завершился с ошибкой'), 500
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
    info = updater.check(VERSION, github_open, UPDATE_API)
    app_update['latest'] = info
    cache = read_update_cache()
    cache.update(checked=time.time(), latest=info)
    if info and notify_new and cache.get('notified') != info['version']:
        cache['notified'] = info['version']
        notify(f'Вышла версия {info["version"]}. Обновить можно на странице программы — одной кнопкой.')
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
        root = updater.download(info, github_open, UPDATE_DIR, lambda done, total: app_update.update(done=done, total=total))
        script = UPDATE_DIR / 'updater.py'  # копия: свой файл updater.py тоже заменится
        shutil.copy2(BASE / 'updater.py', script)
        subprocess.Popen([sys.executable, str(script), '--base', str(BASE), '--work', str(UPDATE_DIR),
                          '--new', str(root), '--version', info['version'], '--old-version', VERSION,
                          '--pid', str(os.getpid()), '--port', str(PORT)],
                         cwd=UPDATE_DIR, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except ValueError as e:
        app_update.update(state='error', error=str(e))
        return
    except Exception as e:  # noqa: BLE001
        app_update.update(state='error', error=f'не удалось скачать обновление: {e}')
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
        return jsonify(error=f'Не удалось связаться с GitHub: {e}'), 502
    return jsonify(app_update_state())


@app.post('/api/app-update')
def api_app_update_install():
    if DEV_COPY:
        return jsonify(error='Это папка разработки (git) — обновляйте её через git pull.'), 409
    if not app_update['latest']:
        return jsonify(error='Новой версии нет.'), 400
    if app_update['state'] in ('downloading', 'restarting'):
        return jsonify(app_update_state())
    if any(t['status'] in ('running', 'queued') for t in list(tasks.values())):
        return jsonify(error='Сначала дождитесь конца загрузок или отмените их: при обновлении программа перезапускается.'), 409
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
        return jsonify(error='Вставьте ссылку или напишите название песни.'), 400
    if not URL_RE.match(url):
        exclude = [x for x in data.get('exclude') or [] if isinstance(x, str)][:500]
        try:
            results, failed = search(url, exclude)
        except Exception as e:  # noqa: BLE001
            return error_response(e)
        if not results and not exclude:
            return jsonify(error='Ничего не найдено. Попробуйте написать иначе.'), 404
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
        options = [{'id': f'v{h}', 'kind': 'video', 'label': f'до {h}p', 'note': 'MP4'}
                   for h in (1080, 720, 480)]
        return jsonify(
            url=url, playlist=True, count=len(entries),
            title=info.get('title') or 'Плейлист',
            site=info.get('extractor_key') or info.get('webpage_url_domain'),
            uploader=info.get('uploader') or info.get('channel'),
            thumbnail=thumb, duration=None,
            options=options + AUDIO_OPTIONS,
        )

    return jsonify(
        url=info.get('webpage_url') or url, playlist=False, count=1,
        title=info.get('title') or 'Без названия',
        site=info.get('extractor_key') or info.get('webpage_url_domain'),
        uploader=info.get('uploader') or info.get('channel'),
        thumbnail=info.get('thumbnail'),
        duration=info.get('duration'),
        live=bool(info.get('is_live')),
        options=video_options(info) + AUDIO_OPTIONS,
    )


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
                raise StreamError('Послушать можно одну песню, а это плейлист.')
            if info.get('is_live'):
                raise StreamError('Прямой эфир здесь не послушать.')
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
        raise StreamError('Не нашёл звук по этой ссылке.')
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
                raise StreamError('Послушать можно одну песню, а это плейлист.')
            if info.get('is_live'):
                raise StreamError('Прямой эфир здесь не послушать.')
            if (info.get('duration') or 0) > LISTEN_MAX:
                raise StreamError(f'Запись длиннее {LISTEN_MAX // 60} минут с этого сайта не послушать — её можно скачать.')
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
            raise StreamError('Не нашёл звук по этой ссылке.')
        out = folder / 'listen.m4a'
        for audio in (['-c:a', 'copy'], ['-c:a', 'aac', '-b:a', '160k']):  # не всякий звук ложится в M4A как есть
            done = subprocess.run([str(FFMPEG), '-hide_banner', '-nostdin', '-y', '-loglevel', 'error', '-i', str(src),
                                   '-vn', *audio, '-movflags', '+faststart', str(out)], capture_output=True)
            if done.returncode == 0:
                break
        else:
            raise StreamError('Не получилось подготовить звук для прослушивания — скачайте файл.')
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
        return jsonify(error='Нужна ссылка на песню или видео.'), 400
    try:
        return jsonify(ok=True, **find_stream(url)['meta'])
    except StreamError as e:
        return jsonify(error=str(e)), 400


@app.get('/api/stream')
def api_stream():
    """Звук песни для <audio>: с поддержкой Range, чтобы работала перемотка."""
    url = request.args.get('url', '').strip()
    if not URL_RE.match(url):
        return jsonify(error='Нужна ссылка на песню или видео.'), 400
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
                return jsonify(error='Файл для прослушивания пропал — включите песню ещё раз.'), 410
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
            return jsonify(error=f'Сайт не отвечает: {e}'), 502

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
        return None, 'Неизвестный вариант.'
    folder = Path(text_arg(data, 'dir')) if text_arg(data, 'dir') else download_dir()
    if not folder.is_absolute() or not folder.is_dir():
        return None, f'Папка не найдена: {folder}'
    return folder, None


def parse_section(data):
    """Начало и конец фрагмента в секундах → (start, end), None или текст ошибки."""
    start, end = data.get('start'), data.get('end')
    if start in (None, '') and end in (None, ''):
        return None
    try:
        start = float(start or 0)
        end = float(end) if end not in (None, '') else float(data.get('duration') or 0)
    except (TypeError, ValueError):
        return 'Время фрагмента указано неверно.'
    if not (math.isfinite(start) and math.isfinite(end)):
        return 'Время фрагмента указано неверно.'
    if not end:
        return 'Укажите, где фрагмент заканчивается.'
    if start < 0 or end <= start:
        return 'Конец фрагмента должен быть позже начала.'
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
        'playlist': bool(data.get('playlist')) and not from_list,
        'count': data.get('count') if isinstance(data.get('count'), int) else 0,
        'dir': str(folder),
        'section': section, 'from_list': from_list,
        'status': 'running', 'stage': 'Подготовка', 'percent': 0,
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
        return jsonify(error='Нужна ссылка на видео.'), 400
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
        return jsonify(error='Не нашёл ни одной ссылки.'), 400
    seq = next(request_seq)
    ids = []
    for i, (url, title) in enumerate(urls.items()):
        task = new_task(data, folder, url, title, seq, i, from_list=True)
        task['status'], task['stage'] = 'queued', 'В очереди'
        download_queue.put(task)
        ids.append(task['id'])
    return jsonify(ids=ids)


def queue_worker():
    while True:
        task = download_queue.get()
        with tasks_lock:
            if task['status'] != 'queued':  # отменили, пока ждала очереди
                continue
            task['status'], task['stage'] = 'running', 'Подготовка'
        try:
            run_task(task)
        except Exception as e:  # noqa: BLE001 — иначе поток очереди умрёт и остальные ссылки зависнут
            task['status'], task['stage'], task['error'] = 'error', 'Ошибка', str(e)


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
    return f'{num / 1024 / 1024:.0f} МБ' if num >= 10 * 1024 * 1024 else f'{num / 1024 / 1024:.1f} МБ'


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
                task['item'] = f'{i} из {len(task["sources"])}'
            task['percent'] = 0
            path, before, after = compress_file(task, src)
            if path:
                task['files'].append(path)
                total_before, total_after = total_before + before, total_after + after
            else:
                kept += 1
        task['status'], task['stage'], task['percent'] = 'done', 'Готово', 100
        if task['files']:
            saved = round((1 - total_after / total_before) * 100)
            task['label'] = f'Сжато: {size_mb(total_before)} → {size_mb(total_after)} (меньше на {saved}%)'
            if kept:
                task['label'] += f' · {kept} уже были сжаты хорошо'
        else:
            task['label'] = 'Видео уже сжато хорошо — меньше не получилось, оставил как было'
    except Cancelled:
        task['status'], task['stage'] = 'cancelled', 'Отменено'
    except Exception as e:  # noqa: BLE001
        if task['cancel']:
            task['status'], task['stage'] = 'cancelled', 'Отменено'
        else:
            task['status'], task['stage'] = 'error', 'Ошибка'
            task['error'], task['error_detail'] = 'Не удалось сжать видео.', str(e)
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
            task['status'], task['stage'] = 'running', 'Сжимаю'
        run_compress(task)


threading.Thread(target=compress_worker, daemon=True).start()


@app.post('/api/tasks/<task_id>/compress')
def api_compress(task_id):
    source = tasks.get(task_id)
    files = [f for f in (source or {}).get('files', [])
             if Path(f).suffix.lower() in VIDEO_EXTS and os.path.exists(f)]
    if not source or source['status'] != 'done' or not files:
        return jsonify(error='Сжать можно только скачанное видео, которое лежит на месте.'), 400
    task = new_task({'option': 'compress'}, Path(files[0]).parent, '', source['title'], next(request_seq))
    task.update(kind='compress', label='Сжатие видео', status='queued', stage='В очереди',
                source=task_id, sources=files)
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
            task['status'], task['stage'] = 'cancelled', 'Отменено'
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
    """Убирает из списка всё, что уже не качается и не ждёт очереди."""
    with tasks_lock:
        for task_id in [k for k, t in tasks.items() if t['status'] not in ('running', 'queued')]:
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
                   proxy=s['proxy'], proxy_all=s['proxy_all'], proxy_sites=s['proxy_sites'])


@app.post('/api/settings')
def api_settings_save():
    data = json_body()
    if 'ask_every_time' in data:
        save_settings(ask_every_time=bool(data['ask_every_time']))
    if 'proxy_all' in data:
        save_settings(proxy_all=bool(data['proxy_all']))
    if 'proxy' in data:
        proxy = normalize_proxy(text_arg(data, 'proxy')) if text_arg(data, 'proxy') else ''
        if proxy is None:
            return jsonify(error='Это не похоже на адрес прокси. Пример: socks5://127.0.0.1:10808'), 400
        save_settings(proxy=proxy)
    return api_settings()


@app.post('/api/proxy/check')
def api_proxy_check():
    """С адресом — проверяет этот прокси, без адреса — ищет прокси VPN-программы на компьютере."""
    text = text_arg(json_body(), 'proxy')
    if text:
        proxy = normalize_proxy(text)
        if proxy is None:
            return jsonify(error='Это не похоже на адрес прокси. Пример: socks5://127.0.0.1:10808'), 400
        if not proxy_alive(proxy):
            return jsonify(ok=False, error='По этому адресу никто не отвечает. Включена ли VPN-программа?')
        if not proxy_works(proxy):
            return jsonify(ok=False, error='Прокси отвечает, но YouTube через него не открылся.')
    else:
        proxy = find_proxy()
        if not proxy:
            return jsonify(ok=False, error='Прокси VPN-программы на компьютере не найден. Включите VPN-программу '
                                           'или впишите адрес из её настроек (раздел «Прокси», «SOCKS», «Порт»).')
    save_settings(proxy=proxy)
    return jsonify(ok=True, proxy=proxy)


# ---------- открытые вкладки программы ----------
# Каждая открытая страница держит поток событий /api/events. Повторный запуск (ярлык, «Открыть» у часов)
# спрашивает страницы «есть кто?» и, если какая-то отозвалась, новую вкладку не открывает.
pages = set()                     # очереди сообщений для открытых страниц
pages_lock = threading.Lock()
page_answered = threading.Event()
tray_icon = None


@app.get('/api/events')
def api_events():
    q = queue.Queue()
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
            tray_icon.notify(text, 'Универсальный скачиватель')
        except Exception:  # noqa: BLE001
            pass


def show_page():
    """Открывает страницу программы, только если её нет ни в одной вкладке. Если есть — вкладка мигает
    заголовком, а у часов всплывает подсказка. True — открыли новую вкладку."""
    page_answered.clear()
    if tell_pages('ping') and page_answered.wait(2):
        tell_pages('here')
        notify('Программа уже открыта — во вкладке браузера «Универсальный скачиватель».')
        return False
    webbrowser.open(f'http://{HOST}:{PORT}')
    return True


@app.post('/api/show')
def api_show():
    """Программу запустили ещё раз (второй процесс) — показываем страницу."""
    return jsonify(opened=show_page())


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
    if TRAY and os.name == 'nt':
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, 'Универсальный скачиватель', 0x10)


BUSY_TEXT = ('Порт {port} занят другой программой, поэтому скачиватель не запустился. '
             'Поменяйте «port» в settings.json (например, на 8766) и запустите снова.')


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
        pystray.MenuItem('Открыть', open_page, default=True),  # и по щелчку на значке
        pystray.MenuItem('Папка загрузок', open_downloads),
        pystray.MenuItem(lambda item: f'Обновить до версии {app_update["latest"]["version"]}' if app_update['latest'] else '',
                         open_page, visible=lambda item: bool(app_update['latest']) and not DEV_COPY),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('Выход', quit_app),
    )

    def setup(icon):
        icon.visible = True
        result = app_update['result']
        if result:
            notify(f'Программа обновлена до версии {result["to"]}.' if result.get('ok')
                   else f'Обновление не установилось: {result.get("error")}')

    tray_icon = pystray.Icon('downloader', Image.open(ICON), 'Универсальный скачиватель', menu)
    tray_icon.run(setup)


def main():
    url = f'http://{HOST}:{PORT}'
    owner = port_owner()
    if owner == 'ours':  # уже запущено — открываем страницу, только если её нет ни в одной вкладке
        if '--no-browser' not in sys.argv:
            req = urllib.request.Request(f'{url}/api/show', b'{}', {'Content-Type': 'application/json'})
            try:
                urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=10).close()
            except OSError:
                webbrowser.open(url)
        return
    if owner == 'other':
        fail(BUSY_TEXT.format(port=PORT))
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
        fail(BUSY_TEXT.format(port=PORT))
        return
    if '--no-browser' not in sys.argv:
        threading.Thread(target=open_page_later, daemon=True).start()
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
