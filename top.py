"""Вкладка «Топ»: топ-20 — русские (Яндекс Музыка, запасной — zaycev.net) и иностранные (Spotify, мир, по данным kworb.net).

Только открытые страницы, без входа и ключей. Список обновляется раз в сутки в фоне: кеш лежит в памяти и в файле,
поэтому при открытии вкладки он есть сразу. Если один источник не ответил — работает второй, а у этого остаётся
прошлый список (или «источник недоступен», если прошлого нет). Сеть — забота app.py (прокси, DNS): сюда передают opener(url) → bytes.
"""
import concurrent.futures
import html
import json
import re
import threading
import time
import urllib.parse
from pathlib import Path

COUNT = 20
DAY = 24 * 3600
RETRY = 15 * 60  # после неудачи пробуем не чаще
CACHE_VER = 2  # сменился источник или поля — старый кеш не берём
YANDEX = 'https://api.music.yandex.net/landing3/chart/russia'
ZAYCEV = 'https://zaycev.net/popular/index.html?popular-sort=week'
KWORB = 'https://kworb.net/spotify/country/global_weekly.html'
OEMBED = 'https://open.spotify.com/oembed?url='

TAG = re.compile(r'<[^>]+>')
# одна запись zaycev: название, исполнитель, обложка 100 px (больше на сайте нет)
Z_TITLE = re.compile(r'data-qa="track-link"[^>]*><span>(.*?)</span>', re.S)
Z_ARTIST = re.compile(r'data-qa="artist-link"[^>]*><span>(.*?)</span>', re.S)
Z_COVER = re.compile(r'<img src="(https://[^"]+)"')
# строка kworb: место, исполнитель и название (ссылки) с припиской «(w/ …)», недельные прослушивания
K_ROW = re.compile(r'<tr[^>]*><td class="np">(\d+)</td>(.*?)</tr>', re.S)
K_TEXT = re.compile(r'<td class="text mp"><div>(.*?)</div></td>', re.S)
K_TRACK = re.compile(r'href="\.\./track/([A-Za-z0-9]+)\.html"')
K_CELLS = re.compile(r'<td[^>]*>(.*?)</td>', re.S)
K_DATE = re.compile(r'Chart - Global - (\d{4})/(\d{2})/(\d{2})')

lock = threading.Lock()
state = {k: {'items': [], 'asof': '', 'src': '', 'updated': 0.0, 'tried': 0.0, 'busy': False} for k in ('ru', 'world')}
cache_file = None
opener = None  # opener(url) → bytes


def clean(text):
    return re.sub(r'\s+', ' ', html.unescape(TAG.sub('', text))).strip()


def query_of(artist, title):
    """Что подставить в поиск вкладки «Музыка»: «исполнитель — название»."""
    return f'{artist} — {title}'


def fetch_yandex():
    """Яндекс Музыка, чарт России: обновляется каждый день, listeners — слушатели за сутки."""
    tracks = json.loads(opener(YANDEX))['result']['chart']['tracks']
    items = []
    for t in tracks:
        tr = t.get('track') or {}
        names = [a.get('name', '') for a in tr.get('artists') or [] if a.get('name')]
        title = (tr.get('title') or '').strip()
        if not names or not title:
            continue
        if tr.get('version'):
            title += f" ({tr['version'].strip()})"
        cover = tr.get('coverUri') or ''
        items.append({'pos': len(items) + 1, 'artist': ', '.join(names), 'title': title,
                      'query': query_of(names[0], title), 'plays': int((t.get('chart') or {}).get('listeners') or 0),
                      'per': 'day', 'cover': 'https://' + cover.replace('%%', '200x200') if cover else ''})
        if len(items) == COUNT:
            break
    if len(items) < 3:
        raise ValueError('yandex: чарт не найден')
    return items, '', 'yandex'


def fetch_ru():
    """Яндекс Музыка; не ответил — zaycev.net."""
    try:
        return fetch_yandex()
    except Exception:  # noqa: BLE001
        return fetch_zaycev()


def fetch_zaycev():
    """zaycev.net, неделя: порядок — как в списке на странице (в данных страницы он не сохранён)."""
    page = opener(ZAYCEV).decode('utf-8', 'replace')
    items = []
    for chunk in page.split('<li><div data-qa="track"')[1:]:
        title = Z_TITLE.search(chunk)
        artist = Z_ARTIST.search(chunk)
        if not title or not artist:
            continue
        cover = Z_COVER.search(chunk)
        name = re.sub(r'^By\s+', '', clean(artist.group(1)))  # на zaycev у части исполнителей приставка «By»
        items.append({'pos': len(items) + 1, 'artist': name, 'title': clean(title.group(1)),
                      'cover': cover.group(1) if cover else ''})
        if len(items) == COUNT:
            break
    if len(items) < 3:
        raise ValueError('zaycev: список не найден')
    for it in items:
        it['query'] = query_of(it['artist'], it['title'])
    return items, '', 'zaycev'


def spotify_cover(track_id):
    try:
        data = json.loads(opener(OEMBED + urllib.parse.quote(f'https://open.spotify.com/track/{track_id}', safe='')))
        url = data.get('thumbnail_url') or ''
        return url if url.startswith('https://') else ''
    except Exception:  # noqa: BLE001 — без обложки песня остаётся в списке
        return ''


def fetch_world():
    """kworb.net: Spotify, мир, недельный чарт; в колонке Streams — прослушивания за неделю."""
    page = opener(KWORB).decode('utf-8', 'replace')
    items, ids = [], []
    for m in K_ROW.finditer(page):
        text = K_TEXT.search(m.group(2))
        if not text:
            continue
        links = re.findall(r'<a [^>]*>(.*?)</a>', text.group(1), re.S)
        if len(links) < 2:
            continue
        artist, title = clean(links[0]), clean(links[1])
        feat = clean(text.group(1).split('</a>', 2)[-1])  # «(w/ A, B)» после названия
        feat = re.sub(r'^\(\s*(w/|with|feat\.?)\s*|\)\s*$', '', feat, flags=re.I).strip()
        cells = [clean(c) for c in K_CELLS.findall(m.group(2))]
        streams = int(re.sub(r'\D', '', cells[5]) or 0) if len(cells) > 5 else 0
        track = K_TRACK.search(text.group(1))
        ids.append(track.group(1) if track else '')
        items.append({'pos': int(m.group(1)), 'artist': artist + (f', {feat}' if feat else ''), 'title': title,
                      'query': query_of(artist, title), 'plays': streams, 'per': 'week', 'cover': ''})
        if len(items) == COUNT:
            break
    if len(items) < 3:
        raise ValueError('kworb: таблица не найдена')
    with concurrent.futures.ThreadPoolExecutor(5) as pool:  # обложки с Spotify (oEmbed, без ключа)
        for it, cover in zip(items, pool.map(lambda i: spotify_cover(i) if i else '', ids)):
            it['cover'] = cover
    date = K_DATE.search(page)
    return items, f'{date.group(1)}-{date.group(2)}-{date.group(3)}' if date else '', 'kworb'


FETCH = {'ru': fetch_ru, 'world': fetch_world}


def save():
    if not cache_file:
        return
    try:
        data = {k: {f: v[f] for f in ('items', 'asof', 'src', 'updated')} for k, v in state.items()}
        Path(cache_file).write_text(json.dumps({'v': CACHE_VER, **data}, ensure_ascii=False), encoding='utf-8')
    except OSError:
        pass


def refresh(key):
    try:
        items, asof, src = FETCH[key]()
        with lock:
            state[key].update(items=items, asof=asof, src=src, updated=time.time())
    except Exception:  # noqa: BLE001 — источник недоступен или вёрстка изменилась: остаётся прошлый список
        pass
    with lock:
        state[key].update(tried=time.time(), busy=False)
    save()


def start_stale():
    """Запускает в фоне обновление тех списков, которым больше суток (или которых нет)."""
    now = time.time()
    with lock:
        due = [k for k, s in state.items()
               if not s['busy'] and now - s['updated'] > DAY and now - s['tried'] > RETRY]
        for k in due:
            state[k]['busy'] = True
    for k in due:
        threading.Thread(target=refresh, args=(k,), daemon=True).start()


def init(path, open_url):
    """path — файл кеша; open_url(url) → bytes. Читает кеш и в фоне обновляет устаревшее."""
    global cache_file, opener
    cache_file, opener = path, open_url
    try:
        saved = json.loads(Path(path).read_text(encoding='utf-8'))
        if saved.get('v') != CACHE_VER:
            saved = {}
        for k, s in state.items():
            c = saved.get(k) or {}
            if isinstance(c.get('items'), list):
                s.update(items=c['items'], asof=str(c.get('asof') or ''), src=str(c.get('src') or ''), updated=float(c.get('updated') or 0))
    except (OSError, ValueError, TypeError):
        pass
    start_stale()


def snapshot():
    """Ответ вкладки: на каждый столбец — список (может быть прошлым), loading и failed."""
    start_stale()
    with lock:
        return {k: {'items': s['items'], 'asof': s['asof'], 'src': s['src'], 'updated': int(s['updated']), 'loading': s['busy'],
                    'failed': not s['items'] and not s['busy'] and s['tried'] > 0}
                for k, s in state.items()}
