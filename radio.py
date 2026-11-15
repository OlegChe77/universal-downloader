"""Вкладка «Радио»: все станции мира из Radio Browser (radio-browser.info), российские — первыми.

Список (~50 тысяч станций, ~60 МБ JSON) скачивается раз в сутки в фоне и лежит в памяти и в файле кеша —
сжато, только нужные поля. Страница получает его по частям (query), с фильтром по жанру, стране и строке поиска.
«Сейчас играет» — из ICY-метаданных потока (now): открываем поток с заголовком Icy-MetaData: 1, читаем один
блок и закрываем. У HLS-станций (m3u8) таких данных нет. Сеть для API — забота app.py: opener(url) → bytes.
"""
import concurrent.futures
import http.client
import json
import math
import re
import ssl
import threading
import time
import urllib.parse
from pathlib import Path

DAY = 24 * 3600
RETRY = 15 * 60
CACHE_VER = 1
SERVERS = ['https://de1.api.radio-browser.info', 'https://de2.api.radio-browser.info', 'https://fi1.api.radio-browser.info']
PAGE = 10000  # больше за раз сервер не отдаёт (502)
RU = 'RU'
TOP_COUNTRIES = 10  # в ряду стран: Россия и ещё 9 с наибольшим числом станций
UA = 'UniversalDownloader/1.0'

# Жанры: id, русское и английское название, части тегов Radio Browser (теги — свободный текст через запятую)
GENRES = [
    ('pop', 'Поп', 'Pop', ['pop', 'top 40', 'top40', 'hits', 'chart', 'поп']),
    ('rock', 'Рок', 'Rock', ['rock', 'рок']),
    ('electronic', 'Электроника', 'Electronic', ['electro', 'techno', 'house', 'trance', 'edm', 'drum and bass', 'dnb',
                                                   'dubstep', 'synth', 'electrónica', 'электрон']),
    ('dance', 'Танцевальная', 'Dance', ['dance', 'disco', 'club', 'танц', 'диско']),
    ('retro', 'Ретро', 'Oldies', ['oldies', 'retro', '50s', '60s', '70s', '80s', '90s', 'ретро']),
    ('hiphop', 'Хип-хоп', 'Hip-hop', ['hip hop', 'hip-hop', 'hiphop', 'rap', 'r&b', 'rnb', 'хип-хоп', 'рэп']),
    ('metal', 'Метал', 'Metal', ['metal', 'метал']),
    ('jazz', 'Джаз', 'Jazz', ['jazz', 'swing', 'джаз']),
    ('classical', 'Классика', 'Classical', ['classical', 'klassik', 'opera', 'symphon', 'baroque', 'классич', 'классика']),
    ('news', 'Новости и разговоры', 'News & talk', ['news', 'talk', 'information', 'noticias', 'nachrichten', 'новост', 'разговор']),
    ('chanson', 'Шансон', 'Chanson', ['chanson', 'шансон']),
    ('folk', 'Фолк', 'Folk', ['folk', 'фолк', 'народн']),
    ('chill', 'Чилаут', 'Chillout', ['chill', 'lounge', 'ambient', 'relax', 'easy listening', 'downtempo', 'релакс']),
    ('latin', 'Латино', 'Latin', ['latin', 'salsa', 'reggaeton', 'bachata', 'cumbia', 'merengue', 'tropical']),
    ('country', 'Кантри', 'Country', ['country', 'кантри']),
    ('blues', 'Блюз', 'Blues', ['blues', 'soul', 'funk', 'блюз']),
    ('reggae', 'Регги', 'Reggae', ['reggae', 'ska', 'dancehall']),
    ('kids', 'Детское', 'Kids', ['kids', 'children', 'детск']),
    ('sport', 'Спорт', 'Sports', ['sport', 'спорт']),
    ('religious', 'Религиозное', 'Religious', ['christian', 'religio', 'gospel', 'catholic', 'church', 'worship',
                                                 'islam', 'quran', 'bible', 'православ']),
]
GENRE_IDS = {g[0] for g in GENRES}
# часть тега — с начала слова: «ska» не найдётся в «alaska», а «электрон» найдётся в «электронная»
GENRE_RX = [(g[0], re.compile(r'(?<![a-zа-яё])(?:' + '|'.join(map(re.escape, g[3])) + ')')) for g in GENRES]
# Страны, где станции чаще передают названия песен в cp1251, а не в UTF-8
CYRILLIC = {'RU', 'UA', 'BY', 'KZ', 'KG', 'TJ', 'UZ', 'BG', 'RS', 'MK', 'MN', 'MD', 'AM', 'GE', 'AZ', 'TM'}

lock = threading.Lock()
state = {'stations': [], 'updated': 0.0, 'tried': 0.0, 'busy': False}
by_id = {}
views = {}  # (жанр, страна, поиск) → отфильтрованный список; сбрасывается при новом списке
cache_file = None
opener = None   # opener(url) → bytes
proxy_of = None  # proxy_of(url) → 'http://host:port' или None — для «сейчас играет»
now_cache = {}  # id станции → (название песни, до какого времени)
now_pool = concurrent.futures.ThreadPoolExecutor(16, thread_name_prefix='icy')


def genres_of(tags):
    """Жанры станции по её тегам (id из GENRES, в порядке тегов: первый — главный)."""
    out = []
    for tag in tags:
        for gid, rx in GENRE_RX:
            if gid not in out and rx.search(tag):
                out.append(gid)
    return out


def compact(s):
    """Станция Radio Browser → только нужные поля (так кеш меньше в несколько раз)."""
    url = (s.get('url_resolved') or s.get('url') or '').strip()
    name = re.sub(r'\s+', ' ', s.get('name') or '').strip()
    if not url.startswith(('http://', 'https://')) or not name:
        return None
    fav = (s.get('favicon') or '').strip()
    tags = [t.strip().lower() for t in (s.get('tags') or '').split(',') if t.strip()][:12]
    return {
        'id': s.get('stationuuid') or '', 'n': name, 'u': url,
        'f': fav if fav.startswith(('http://', 'https://')) else '',
        'c': (s.get('countrycode') or '').upper()[:2], 'cn': (s.get('country') or '').strip(),
        's': (s.get('state') or '').strip(), 't': tags, 'g': genres_of(tags),
        'cd': (s.get('codec') or '').strip(), 'br': int(s.get('bitrate') or 0), 'h': int(s.get('hls') or 0),
        'k': int(s.get('clickcount') or 0) * 3 + int(s.get('votes') or 0),
    }


def fetch_server(base):
    total = json.loads(opener(base + '/json/stats')).get('stations') or 0
    pages = max(1, math.ceil(int(total) / PAGE) + 1)  # +1: станции добавляются, пока качаем

    def page(i):
        q = urllib.parse.urlencode({'hidebroken': 'true', 'order': 'stationuuid', 'limit': PAGE, 'offset': i * PAGE})
        return json.loads(opener(f'{base}/json/stations/search?{q}'))

    with concurrent.futures.ThreadPoolExecutor(3) as pool:
        chunks = list(pool.map(page, range(pages)))
    seen, out = set(), []
    for chunk in chunks:
        for s in chunk:
            c = compact(s)
            if c and c['id'] not in seen:
                seen.add(c['id'])
                out.append(c)
    if len(out) < 1000:
        raise ValueError('radio-browser: слишком мало станций')
    return out


def fetch():
    err = None
    for base in SERVERS:
        try:
            return fetch_server(base)
        except Exception as e:  # noqa: BLE001 — сервер не ответил: следующий
            err = e
    raise err


def order(stations):
    """Россия сверху, внутри — по популярности (клики за сутки и голоса)."""
    return sorted(stations, key=lambda s: (s['c'] != RU, -s['k'], s['n'].lower()))


def use(stations):
    global by_id
    stations = order(stations)
    with lock:
        state['stations'] = stations
        by_id = {s['id']: s for s in stations}
        views.clear()


def save():
    if not cache_file:
        return
    try:
        with lock:
            data = {'v': CACHE_VER, 'updated': state['updated'], 'stations': state['stations']}
        Path(cache_file).write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    except OSError:
        pass


def refresh():
    try:
        stations = fetch()
        use(stations)
        with lock:
            state['updated'] = time.time()
        ok = True
    except Exception:  # noqa: BLE001 — все серверы недоступны: остаётся прошлый список
        ok = False
    with lock:
        state.update(tried=time.time(), busy=False)
    if ok:
        save()


def start_stale():
    now = time.time()
    with lock:
        if state['busy'] or now - state['updated'] < DAY or now - state['tried'] < RETRY:
            return
        state['busy'] = True
    threading.Thread(target=refresh, daemon=True).start()


def init(path, open_url, proxy_for=None):
    """path — файл кеша; open_url(url) → bytes; proxy_for(url) → прокси для потока или None.
    Ничего не качает и не читает: список нужен, только когда откроют вкладку (load)."""
    global cache_file, opener, proxy_of
    cache_file, opener, proxy_of = path, open_url, proxy_for


loaded = False
load_lock = threading.Lock()


def load():
    """Первый запрос вкладки: читаем кеш (~15 МБ) и, если ему больше суток, обновляем в фоне."""
    global loaded
    with load_lock:
        if not loaded and cache_file:
            loaded = True
            try:
                saved = json.loads(Path(cache_file).read_text(encoding='utf-8'))
                if saved.get('v') == CACHE_VER and isinstance(saved.get('stations'), list):
                    use(saved['stations'])
                    state['updated'] = float(saved.get('updated') or 0)
            except (OSError, ValueError, TypeError):
                pass
    start_stale()


def view(genre, country, q):
    """Станции под фильтр, в порядке списка (Россия сверху). Результат запоминается для «Показать ещё»."""
    key = (genre, country, q)
    with lock:
        hit = views.get(key)
        stations = state['stations']
    if hit is not None:
        return hit
    words = q.lower().split()
    out = [s for s in stations
           if (not genre or genre in s['g']) and (not country or s['c'] == country)
           and (not words or all(w in f"{s['n']} {s['s']} {s['cn']} {' '.join(s['t'])}".lower() for w in words))]
    with lock:
        if len(views) > 40:
            views.clear()
        views[key] = out
    return out


def counts(genre, country, q):
    """Сколько станций в каждом жанре (с выбранной страной) и в каждой стране (с выбранным жанром)."""
    g_count, c_count = {}, {}
    for s in view('', country, q):
        for g in s['g']:
            g_count[g] = g_count.get(g, 0) + 1
    for s in view(genre, '', q):
        if s['c']:
            c_count[s['c']] = c_count.get(s['c'], 0) + 1
    genres = [{'id': '', 'n': len(view('', country, q))}] + sorted(
        ({'id': g[0], 'ru': g[1], 'en': g[2], 'n': g_count.get(g[0], 0)} for g in GENRES), key=lambda x: -x['n'])
    countries = sorted(({'cc': cc, 'n': n} for cc, n in c_count.items()), key=lambda x: (x['cc'] != RU, -x['n']))
    return genres, countries, len(view(genre, '', q))


def public(s):
    return {'id': s['id'], 'name': s['n'], 'url': s['u'], 'favicon': s['f'], 'cc': s['c'], 'country': s['cn'],
            'city': s['s'], 'genre': s['g'][0] if s['g'] else '', 'tag': s['t'][0] if s['t'] else '',
            'codec': s['cd'], 'bitrate': s['br'], 'hls': bool(s['h'] or s['u'].split('?')[0].endswith('.m3u8'))}


def query(genre='', country='', q='', offset=0, limit=50, group=False, per_group=5, with_counts=True):
    """Ответ вкладки. group — блоки по странам (для выбранного жанра): offset/limit — по странам."""
    load()
    genre = genre if genre in GENRE_IDS else ''
    country = country.upper()[:2]
    q = q.strip()[:100]
    with lock:
        info = {'updated': int(state['updated']), 'loading': state['busy'], 'count': len(state['stations'])}
    info['failed'] = not info['count'] and not info['loading'] and state['tried'] > 0
    if not info['count']:
        return {**info, 'total': 0, 'items': [], 'groups': [], 'genres': [], 'countries': []}
    stations = view(genre, country, q)
    out = {**info, 'total': len(stations)}
    if with_counts:
        out['genres'], out['countries'], out['all_countries'] = counts(genre, country, q)
    if group:
        by_cc = {}
        for s in stations:
            by_cc.setdefault(s['c'], []).append(s)
        ccs = sorted(by_cc, key=lambda cc: (cc != RU, -len(by_cc[cc]), cc))
        out['groups_total'] = len(ccs)
        out['groups'] = [{'cc': cc, 'country': by_cc[cc][0]['cn'], 'total': len(by_cc[cc]),
                          'items': [public(s) for s in by_cc[cc][:per_group]]}
                         for cc in ccs[offset:offset + limit]]
    else:
        out['items'] = [public(s) for s in stations[offset:offset + limit]]
    return out


def station(sid):
    with lock:
        return by_id.get(sid)


# ---------- «Сейчас играет»: ICY-метаданные ----------
class IcyResponse(http.client.HTTPResponse):
    """SHOUTcast 1 отвечает «ICY 200 OK» вместо «HTTP/1.0 200 OK» — http.client такое не принимает."""

    def _read_status(self):
        line = str(self.fp.readline(65537), 'iso-8859-1')
        if not line:
            raise http.client.RemoteDisconnected('нет ответа')
        if line.startswith('ICY'):
            line = 'HTTP/1.0' + line[3:]
        parts = line.split(None, 2)
        if len(parts) < 2 or not parts[0].startswith('HTTP/'):
            raise http.client.BadStatusLine(line)
        return parts[0], int(parts[1]), parts[2] if len(parts) > 2 else ''


def open_icy(url, proxy, hops=3):
    for _ in range(hops):
        p = urllib.parse.urlsplit(url)
        https = p.scheme == 'https'
        port = p.port or (443 if https else 80)
        ctx = ssl.create_default_context() if https else None
        if ctx:
            ctx.check_hostname = False  # у многих станций просроченный или чужой сертификат; это лишь название песни
            ctx.verify_mode = ssl.CERT_NONE
        if proxy:
            pp = urllib.parse.urlsplit(proxy)
            conn = (http.client.HTTPSConnection(pp.hostname, pp.port, timeout=6, context=ctx) if https
                    else http.client.HTTPConnection(pp.hostname, pp.port, timeout=6))
            conn.set_tunnel(p.hostname, port)
        else:
            conn = (http.client.HTTPSConnection(p.hostname, port, timeout=6, context=ctx) if https
                    else http.client.HTTPConnection(p.hostname, port, timeout=6))
        conn.response_class = IcyResponse
        path = (p.path or '/') + (f'?{p.query}' if p.query else '')
        conn.request('GET', path, headers={'Icy-MetaData': '1', 'User-Agent': UA, 'Accept': '*/*'})
        resp = conn.getresponse()
        if resp.status in (301, 302, 303, 307, 308) and resp.getheader('Location'):
            url = urllib.parse.urljoin(url, resp.getheader('Location'))
            conn.close()
            continue
        return conn, resp
    raise OSError('слишком много перенаправлений')


def read_exact(resp, n):
    buf = b''
    while len(buf) < n:
        part = resp.read(min(n - len(buf), 65536))
        if not part:
            raise OSError('поток оборвался')
        buf += part
    return buf


def decode_meta(raw, cc):
    raw = raw.rstrip(b'\0')
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        return raw.decode('cp1251' if cc in CYRILLIC else 'latin-1', 'replace')


JUNK = re.compile(r'^[\s\-–—_.|:]*$|^(unknown|untitled|n/?a|null|none|advert\w*|реклама|-\s*-)$', re.I)


SITE = re.compile(r'^(https?://)?(www\.)?[\w-]+(\.[\w-]+)+(/\S*)?$', re.I)  # вместо песни — адрес сайта станции


def clean_title(text, s):
    text = re.sub(r'\s+', ' ', text).strip().strip('\'"').strip()
    text = re.sub(r'\s+-\s+', ' — ', text, count=1)
    if JUNK.match(text) or SITE.match(text) or text.lower() == s['n'].lower():
        return ''
    return text[:200]


def read_now(s):
    """Название песни из потока станции; '' — станция его не передаёт."""
    proxy = proxy_of(s['u']) if proxy_of else None
    if proxy and not proxy.startswith('http://'):
        proxy = None  # socks здесь не умеем; пробуем напрямую
    conn, resp = open_icy(s['u'], proxy)
    try:
        metaint = int(resp.getheader('icy-metaint') or 0)
        if resp.status != 200 or metaint <= 0 or metaint > 256 * 1024:
            return ''
        for _ in range(3):  # первый блок бывает пустым — название приходит со следующим
            read_exact(resp, metaint)
            size = read_exact(resp, 1)[0] * 16
            if not size:
                continue
            meta = decode_meta(read_exact(resp, size), s['c'])
            m = re.search(r"StreamTitle='(.*?)';(?=\s*\w+=|\s*$)", meta, re.S) or re.search(r"StreamTitle='(.*?)';", meta, re.S)
            return clean_title(m.group(1), s) if m else ''
        return ''
    finally:
        conn.close()


def now_one(sid, fresh=False):
    s = station(sid)
    if not s:
        return ''
    if s['h'] or s['u'].split('?')[0].endswith('.m3u8'):
        return None  # HLS: метаданных в потоке нет
    hit = now_cache.get(sid)  # (песня, до какого времени, когда узнали)
    if hit and hit[1] > time.time() and not (fresh and time.time() - hit[2] > 15):
        return hit[0]
    try:
        title = read_now(s)
        ttl = 40 if title else 300
    except Exception:  # noqa: BLE001 — станция не ответила
        title, ttl = '', 120
    now_cache[sid] = (title, time.time() + ttl, time.time())
    if len(now_cache) > 3000:
        for k in [k for k, v in now_cache.items() if v[1] < time.time()]:
            now_cache.pop(k, None)
    return title


def now(ids, fresh=False):
    """{id: «исполнитель — песня» | '' (не передаёт) | None (HLS)} для нескольких станций сразу.
    fresh — кнопка «Обновить»: узнать заново, если прошлому ответу больше 15 секунд."""
    ids = [i for i in dict.fromkeys(ids) if i][:30]
    return dict(zip(ids, now_pool.map(lambda i: now_one(i, fresh), ids)))
