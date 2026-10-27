"""Свои разборщики сайтов для yt-dlp. register() ставит их перед встроенными.

- ЯП файлы (yapfiles.ru): встроенный YapFilesIE помечен сломанным — берёт устаревший md5 (403)
  и не знает страниц /show/. md5 у каждого ролика свой, он есть на странице плеера get_player.
- ЯПлакалъ (yaplakal.com): плеер в теме — <div id="yfp-<пост>-<id>" rel="yapfiles">, iframe
  рисует JS, поэтому общий разборщик его не видит и берёт мусорный <audio> (→ HTTP 404).
- Одноклассники (ok.ru): встроенный падает на новом формате плеера (см. OdnoklassnikiIE).
- COMDI (comdi.com): эфиры и их записи, плеер встраивается на сайты скриптом embed-player.js
  с data-watchKey — без своего разборщика «Unsupported URL» (см. ComdiIE).
- Instagram: карусель с фото падала целиком, а называлось всё «Video by …» (см. InstagramIE).
- Встроенный плеер с другого сайта (iframe): общий разборщик заходит только в плееры известных
  сайтов. Если на странице больше ничего нет — идём в iframe сами (см. GenericIE). Так берутся
  KVS-плееры (kvsplayer.com, на многих сайтах «для взрослых») и другие плееры с чужих доменов.
- DTF и vc.ru, d3.ru — «Unsupported URL» (см. OsnovaIE, D3IE).
- Плееры фильмов и сериалов CCDN, ShizaHD, VeoVeo (kinogo и похожие сайты): см. BalancerIE и player_list.
"""
import base64
import json
import re
import time
from collections import Counter
from urllib.parse import parse_qs, urljoin, urlparse

from yt_dlp.extractor import gen_extractor_classes, import_extractors
from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.extractor.generic import GenericIE as _GenericIE
from yt_dlp.extractor.instagram import InstagramIE as _InstagramIE
from yt_dlp.extractor.odnoklassniki import OdnoklassnikiIE as _OdnoklassnikiIE
from yt_dlp.globals import extractors as _extractors
from yt_dlp.networking import Request
from yt_dlp.utils import (ExtractorError, clean_html, determine_ext, extract_attributes, float_or_none,
                          int_or_none, js_to_json, smuggle_url, traverse_obj, unescapeHTML, unsmuggle_url,
                          url_or_none)

from i18n import tr

PLAYER_URL = 'https://api.yapfiles.ru/get_player/?v={}'
# встроенные плееры других сайтов в теме ЯПлакалъ — их разберут свои разборщики yt-dlp
EMBED_HOSTS = re.compile(r'(?:youtube(?:-nocookie)?\.com|youtu\.be|vk(?:video)?\.(?:com|ru)|rutube\.ru'
                         r'|ok\.ru|dzen\.ru|coub\.com|vimeo\.com)/')


class YapFilesIE(InfoExtractor):
    IE_NAME = 'YapFiles'
    _VALID_URL = [
        r'https?://(?:(?:www|api)\.)?yapfiles\.(?:ru|com)/get_player/*\?(?:[^#]*?&)?v=(?P<id>[\w-]+)',
        r'https?://(?:www\.)?yapfiles\.(?:ru|com)/show/(?P<num>\d+)/(?P<hash>[0-9a-f]{32})',
    ]
    _EMBED_REGEX = [r'<iframe\b[^>]+\bsrc=(["\'])(?P<url>(?:https?:)?//(?:(?:www|api)\.)?yapfiles\.(?:ru|com)'
                    r'/get_player/*\?.*?\bv=[\w-]+.*?)\1']

    def _real_extract(self, url):
        m = self._match_valid_url(url).groupdict()
        # id ролика: 'v' + base64('<номер из 8 цифр>-') + 4 первые буквы хэша; страница /show/
        # бывает закрыта для гостей («Вам запрещен доступ»), а плеер отдаёт видео и так
        video_id = m.get('id') or 'v{}{}'.format(
            base64.b64encode(f'{int(m["num"]):08d}-'.encode()).decode(), m['hash'][:4])
        player_url = PLAYER_URL.format(video_id)
        page = self._download_webpage(player_url, video_id, 'Downloading player page')
        load_url = unescapeHTML(self._search_regex(
            r'''(https?://api\.yapfiles\.(?:ru|com)/load/[^'"\s]+)''', page, 'player data url'))
        player = self._download_json(load_url, video_id, headers={'Referer': player_url})['player']

        title = player.get('title') or video_id
        thumbnail = url_or_none(player.get('poster'))
        if title == 'Ролик удален' or 'deleted.jpg' in (thumbnail or ''):
            raise ExtractorError(tr('Видео удалено с ЯП файлов', 'The video was deleted from YapFiles'), expected=True)

        width, height = (int_or_none(x) for x in (str(player.get('res') or 'x').split('x') + [None])[:2])
        formats = []
        for format_id, key, quality in (('sd', 'file', 0), ('hd', 'file_hd', 1)):
            file_url = url_or_none(player.get(key))
            if file_url:
                formats.append({
                    'url': file_url, 'format_id': format_id, 'ext': 'mp4', 'quality': quality,
                    'height': int_or_none(player.get('hd')) if key == 'file_hd' else height,
                    'width': width if key == 'file' else None,
                })
        if not formats:
            raise ExtractorError(tr('ЯП файлы не отдали ссылку на видео', 'YapFiles did not give a video link'), expected=True)
        return {
            'id': video_id,
            'title': title,
            'thumbnail': thumbnail,
            'duration': int_or_none(player.get('length')),
            'webpage_url': url_or_none(player.get('link')) or url,
            'formats': formats,
        }


class YaplakalIE(InfoExtractor):
    IE_NAME = 'Yaplakal'
    _VALID_URL = r'https?://(?:\w+\.)?yaplakal\.com/[^?#]*?topic(?P<id>\d+)'

    def _videos(self, html):
        urls = [PLAYER_URL.format(v) for v in re.findall(r'''\bid=["']yfp-\d+-([\w-]+)["']''', html)]
        for src in re.findall(r'''<iframe\b[^>]+\bsrc=["']([^"']+)''', html):
            src = urljoin('https:', unescapeHTML(src))
            if EMBED_HOSTS.search(src):
                urls.append(src)
        return list(dict.fromkeys(urls))

    def _real_extract(self, url):
        topic_id = self._match_id(url)
        page = self._download_webpage(url, topic_id)
        title = re.sub(r'\s*-\s*ЯПлакалъ.*$', '', self._og_search_title(page, default=None)
                       or self._html_extract_title(page, default=None) or topic_id)
        # сначала видео из самого поста (первая таблица entryNNN), комментарии — только если там пусто
        posts = re.split(r'''\bid=["']entry\d+["']''', page)
        urls = self._videos(posts[1]) if len(posts) > 1 else []
        urls = urls or self._videos(page)
        if not urls:
            raise ExtractorError(tr('В этой теме ЯПлакалъ нет видео', 'There is no video in this Yaplakal topic'), expected=True)
        if len(urls) == 1:
            return self.url_result(urls[0], url_transparent=True, title=title, webpage_url=url,
                                   thumbnail=self._og_search_thumbnail(page, default=None))
        return self.playlist_result((self.url_result(u) for u in urls), topic_id, title)


class OdnoklassnikiIE(_OdnoklassnikiIE):
    """ok.ru теперь кладёт flashvars.metadata в плеер готовым объектом, а встроенный
    разборщик ждёт JSON-строку → «the JSON object must be str, bytes or bytearray, not dict»."""

    def _parse_json(self, json_string, *args, **kwargs):
        if isinstance(json_string, (dict, list)):
            return json_string
        return super()._parse_json(json_string, *args, **kwargs)


class InstagramIE(_InstagramIE):
    """Instagram: в карусели фото — элементы без форматов, из-за них падает загрузка всего поста,
    поэтому оставляем только видео (фото программа не качает). Название — начало подписи к посту."""

    def _extract_product(self, product_info, video_id=None, get_comments=True):
        info = super()._extract_product(product_info, video_id, get_comments)
        # вместо «Video by nasa» / «Post by nasa» — начало подписи к посту (как у TikTok)
        caption = (info.get('description') or '').strip().split('\n')[0].strip()
        if caption:
            info['title'] = caption if len(caption) <= 72 else caption[:71].rstrip() + '…'
        if info.get('_type') == 'playlist':
            info['entries'] = [e for e in info['entries'] if e.get('formats')]
            if not info['entries']:
                raise ExtractorError('There is no video in this post', expected=True)
        return info


class ComdiIE(InfoExtractor):
    """COMDI: плеер — SPA, всё про эфир отдаёт /api/player/event/<ключ> (без входа). Ссылку на HLS
    собираем, как плеер (app.js, сервер Flussonic):
    - запись (archiveStatus file — обрезанная, done — целиком):
      <vodProfile.viewUrl>update/<recordUpdatedAt>/<name>[_0].mp4/index.m3u8;
    - идёт эфир: <сервер liveProfile.viewServer>/<name>/index.m3u8;
    - эфир закончен, записи нет, но есть DVR: …/<name>/index-<начало>-<длительность>.m3u8."""
    IE_NAME = 'COMDI'
    _VALID_URL = (r'https?://(?P<host>(?!www\.)[\w-]+\.comdi\.com)/(?:widget/)?(?P<id>[a-z0-9]{8})'
                  r'(?=[/?#]|$)(?:[^#]*?[?&]widgetKey=(?P<widget>\w+))?')
    _EMBED_REGEX = [r'<iframe\b[^>]+\bsrc=(["\'])(?P<url>(?:https?:)?//(?:[\w-]+\.)*comdi\.com/widget/[a-z0-9]{8}/[^"\']*)\1']

    @classmethod
    def _extract_embed_urls(cls, url, webpage):
        yield from super()._extract_embed_urls(url, webpage)
        # <script src="https://broadcast.comdi.com/widget/static/js/embed-player.js" data-watchKey=…>
        for tag in re.findall(r'<script\b[^>]*\bsrc=[^>]*/embed-player\.js[^>]*>', webpage):
            attrs = {k.lower(): v for k, v in extract_attributes(tag).items()}
            key = attrs.get('data-watchkey') or attrs.get('data-eventkey')
            host = re.match(r'(?:https?:)?//([^/]+)', attrs.get('src') or '')
            if key and host:
                widget = attrs.get('data-widgetkey')
                yield f'https://{host[1]}/widget/{key}/broadcast' + (f'?widgetKey={widget}' if widget else '')

    def _stream_url(self, stream):
        name, status = stream.get('name'), str(stream.get('archiveStatus')).lower()
        if not name:
            return None, False
        if status in ('done', 'file'):
            vod = stream.get('vodProfile') or {}
            if vod.get('type') not in (None, 'flussonic') or not url_or_none(vod.get('viewUrl')):
                return None, False
            suffix = '_0' if status == 'file' else ''
            return f'{vod["viewUrl"]}update/{stream.get("recordUpdatedAt")}/{name}{suffix}.mp4/index.m3u8', False
        servers = sorted((stream.get('liveProfile') or {}).get('viewServer') or [], key=lambda s: s.get('order') or 0)
        server = next((s['server'] for s in servers if (s.get('server') or {}).get('type') == 'flussonic'
                       and url_or_none(s['server'].get('viewUrl'))), None)
        if not server:
            return None, False
        sessions = [s for s in stream.get('broadcastSession') or [] if s.get('timeStart')]
        live = bool(sessions) and not sessions[-1].get('timeStop')
        if live:
            return f'{server["viewUrl"]}{name}/index.m3u8', True
        if stream.get('dvr') and sessions:
            start = sessions[0]['timeStart']
            return f'{server["viewUrl"]}{name}/index-{start}-{sessions[-1]["timeStop"] - start}.m3u8', False
        return None, False

    def _real_extract(self, url):
        m = self._match_valid_url(url).groupdict()
        key, api = m['id'], f'https://{m["host"]}/api/player/'
        event = self._download_json(f'{api}event/{key}', key, 'Downloading event info')
        title = event.get('name_ru') or event.get('name_en') or key
        thumbnail = url_or_none((event.get('moduleTheme') or {}).get('poster')) or url_or_none(
            (event.get('moduleSettings') or {}).get('posterImage'))
        streams = sorted(event.get('stream') or [], key=lambda s: s.get('order') or 0)
        if m.get('widget'):  # виджет на сайте показывает только свои залы
            widget = self._download_json(f'{api}event/module/widget/player/{m["widget"]}', key,
                                         'Downloading widget info', fatal=False) or {}
            ids = {str(i) for i in widget.get('streamsIds') or []}
            streams = [s for s in streams if str(s.get('id')) in ids] or streams

        now, entries = int(time.time()), []
        for stream in streams:
            m3u8, live = self._stream_url(stream)
            if not m3u8:
                continue
            stream_id = f'{key}-{stream.get("id")}'
            formats = self._extract_m3u8_formats(m3u8, stream_id, 'mp4', m3u8_id='hls', live=live, fatal=False)
            if not formats:
                continue
            hall = stream.get('description_ru') or stream.get('description_en')
            entries.append({
                'id': stream_id,
                'title': f'{title} — {hall}' if hall and len(streams) > 1 else title,
                'thumbnail': url_or_none(stream.get('posterUrl')) or thumbnail,
                'timestamp': int_or_none(stream.get('timeStart')) or int_or_none(event.get('timeStart')),
                'duration': None if live else int_or_none(stream.get('duration')),
                'is_live': live,
                'formats': formats,
                'http_headers': {'Referer': f'https://{m["host"]}/'},
            })
        if not entries:
            if event.get('private') or (event.get('moduleAuth') or {}).get('require'):
                raise ExtractorError(tr('Эфир COMDI открыт только после регистрации — записи без входа нет',
                                     'This COMDI stream is open only after registration — no recording without signing in'),
                                     expected=True)
            start = int_or_none(event.get('timeStart'))
            if start and start > now:
                raise ExtractorError(tr('Эфир COMDI ещё не начался', 'The COMDI stream has not started yet'), expected=True)
            raise ExtractorError(tr('У эфира COMDI нет записи', 'This COMDI stream has no recording'), expected=True)
        if len(entries) == 1:
            return {**entries[0], 'id': key, 'webpage_url': url}
        return self.playlist_result(entries, key, title)


class OsnovaIE(InfoExtractor):
    """DTF и vc.ru (платформа «Основа»): пост — в window.__INITIAL_STATE__, ключ entry@<id>. Видео поста — блок
    media, картинка с isVideo: true (type gif); файл — leonardo.osnova.io/<uuid>/-/format/mp4/. Ролик с YouTube и
    др. — external_service {name, id}."""
    IE_NAME = 'DTF'
    _VALID_URL = r'https?://(?:www\.)?(?:dtf|vc)\.ru/(?:[^?#]+/)?(?P<id>\d+)(?:-[^/?#]*)?/?(?:[?#]|$)'
    _SERVICES = {'youtube': 'https://www.youtube.com/watch?v={}', 'vimeo': 'https://vimeo.com/{}',
                 'coub': 'https://coub.com/view/{}', 'rutube': 'https://rutube.ru/video/{}/'}

    def _media(self, obj, found):
        if isinstance(obj, dict):
            data = obj.get('data') if obj.get('type') in ('image', 'video') else None
            if isinstance(data, dict) and data.get('uuid'):
                found.setdefault(data['uuid'], data)
            for value in obj.values():
                self._media(value, found)
        elif isinstance(obj, list):
            for value in obj:
                self._media(value, found)

    def _real_extract(self, url):
        post_id = self._match_id(url)
        webpage = self._download_webpage(url, post_id)
        state = self._search_json(r'window\.__INITIAL_STATE__\s*=', webpage, 'state', post_id)
        entry = state.get(f'entry@{post_id}') or {}
        title = entry.get('title') or clean_html(traverse_obj(entry, ('blocks', ..., 'data', 'text'), get_all=False)) \
            or self._og_search_title(webpage, default=None) or post_id
        common = {
            'uploader': traverse_obj(entry, ('author', 'name'), ('subsite', 'name')),
            'timestamp': int_or_none(entry.get('date')),
            'extractor_key': 'vc.ru' if '//vc.ru' in url or '.vc.ru' in url else 'DTF',  # подпись сайта на странице
        }
        found, entries = {}, []
        self._media(entry.get('blocks'), found)
        for uuid, data in found.items():
            service = data.get('external_service')
            if isinstance(service, dict) and service:
                link = url_or_none(service.get('url')) or (
                    self._SERVICES[service['name']].format(service['id'])
                    if service.get('name') in self._SERVICES and service.get('id') else None)
                if link:
                    entries.append(self.url_result(link))
                continue
            if not data.get('isVideo'):
                continue  # картинка или GIF без видео
            entries.append({
                **common,
                'id': uuid,
                'title': title,
                'formats': [{
                    'url': f'https://leonardo.osnova.io/{uuid}/-/format/mp4/',
                    'ext': 'mp4',
                    'width': int_or_none(data.get('width')),
                    'height': int_or_none(data.get('height')),
                    'filesize': int_or_none(data.get('size')),
                    'acodec': None if data.get('has_audio', True) else 'none',
                }],
                'duration': float_or_none(data.get('duration')),
                'thumbnail': f'https://leonardo.osnova.io/{uuid}/-/format/jpg/',
            })
        if not entries:
            raise ExtractorError(tr('В этом посте нет видео', 'There is no video in this post'), expected=True)
        if len(entries) == 1:
            return {**entries[0], 'id': post_id} if entries[0].get('_type') is None else entries[0]
        return self.playlist_result(entries, post_id, title, **common)


class D3IE(InfoExtractor):
    """d3.ru и его сообщества (rukojop.d3.ru…): открытый API d3.ru/api/posts/<id>/. Видео поста — data.link /
    data.media с type video (прямой mp4 на jpg.wtf или ссылка на YouTube и др.), ещё <video>/<iframe> в тексте."""
    IE_NAME = 'd3.ru'
    _VALID_URL = r'https?://(?:[\w-]+\.)?d3\.ru/(?:[^/?#]*-)?(?P<id>\d+)/?(?:[?#]|$)'
    _DIRECT = re.compile(r'\.(?:mp4|webm|mov|m4v)(?:[?#]|$)', re.I)

    def _real_extract(self, url):
        post_id = self._match_id(url)
        post = self._download_json(f'https://d3.ru/api/posts/{post_id}/', post_id)
        data = post.get('data') or {}
        title = post.get('title') or data.get('title') or clean_html(data.get('text')) or post_id
        common = {
            'uploader': traverse_obj(post, ('user', 'login')),
            'timestamp': int_or_none(post.get('created')),
            'extractor_key': 'd3.ru',
        }
        videos = [(m.get('url'), m) for m in (data.get('link'), data.get('media'))
                  if isinstance(m, dict) and m.get('type') == 'video']
        videos += [(unescapeHTML(src), {}) for src in re.findall(
            r'<(?:video|source|iframe)\b[^>]*\bsrc=["\']([^"\']+)', data.get('text') or '')]
        entries, seen = [], set()
        for link, media in videos:
            link = url_or_none(urljoin(url, link or ''))
            if not link or link in seen:
                continue
            seen.add(link)
            if not self._DIRECT.search(link):
                entries.append(self.url_result(link))
                continue
            entries.append({
                **common,
                'id': f'{post_id}-{len(entries) + 1}',
                'title': title,
                'formats': [{
                    'url': link,
                    'ext': determine_ext(link, 'mp4'),
                    'width': int_or_none(media.get('width')),
                    'height': int_or_none(media.get('height')),
                }],
                'thumbnail': url_or_none(traverse_obj(media, ('thumbnails', 'original', 'url'))),
            })
        if not entries:
            raise ExtractorError(tr('В этом посте нет видео', 'There is no video in this post'), expected=True)
        if len(entries) == 1:
            return {**entries[0], 'id': post_id} if entries[0].get('_type') is None else entries[0]
        return self.playlist_result(entries, post_id, title, **common)


def playerjs_decode(text):
    """PlayerJS: «#2» + base64 со вставленным мусором «<разделитель><base64 от 6 букв>» (разделитель у сайта свой:
    //_//, /shiza/…). Мусор бывает вставлен в мусор — вычищаем, пока что-то находится."""
    if not text.startswith('#2'):
        return text
    text = text[2:]
    # с перекрытием: в «/shiz/shiza/…» (мусор, вставленный в разделитель) иначе нашёлся бы только «/shiz/»
    seps = Counter(re.findall(r'(?=(//_//|/[a-z]{2,12}/))', text))
    if seps:
        sep = max(seps, key=lambda s: (seps[s], len(s)))
        while True:  # внешний кусок появляется, только когда убран вставленный в него
            junk = {t for t in re.findall(f'(?={re.escape(sep)}([A-Za-z0-9+/]{{8}}))', text) if _junk_word(t)}
            cleaned = text
            for t in junk:
                cleaned = cleaned.replace(sep + t, '')
            if cleaned == text:
                break
            text = cleaned
    return base64.b64decode(text + '=' * (-len(text) % 4)).decode()


def _junk_word(token):
    try:
        word = base64.b64decode(token).decode('ascii')
    except ValueError:
        return False
    return len(word) == 6 and word.isalnum()


def playerjs_links(text):
    """«[1080p]https://…,[720p]https://…» (адрес может быть с запасным через « or ») → [(подпись, адрес)]."""
    return [(label, link.split(' or ')[0]) for label, link in
            re.findall(r'(?:\[([^\]]*)\])?(https?://.+?)(?=,\[|,https?://|$)', text)]


# язык звуковой дорожки HLS по её названию («01. Дубляж. ТВ-3 (RUS)»), если LANGUAGE в плейлисте нет
TRACK_LANGS = (('ru', r'\(RUS\)|\brus\b|рус'), ('uk', r'\(UKR\)|\bukr\b|укр'), ('en', r'\(ENG\)|\beng\b|англ|оригинал'))


def label_tracks(formats):
    """Дорожкам без языка — язык по названию; первая по номеру — главная (её yt-dlp и возьмёт), из групп
    audio1080/audio720 — та, что для лучшего видео."""
    for f in formats:
        if f.get('vcodec') != 'none':
            continue
        f['acodec'] = f.get('acodec') or 'mp4a'  # у этих CDN звук — AAC; без кодека программа не покажет выбор дорожки
        if f.get('language'):
            continue
        name = f.get('format_note') or f.get('format_id') or ''
        f['language'] = next((lang for lang, rx in TRACK_LANGS if re.search(rx, name, re.I)), None)
        num = re.search(r'(?:^|-)(\d+)\.\s', name)
        if num:
            f['language_preference'] = 10 - min(int(num[1]), 10)
        group = re.search(r'audio(\d+)', f.get('format_id') or '')
        if group:
            f['quality'] = int(group[1])


class BalancerIE(InfoExtractor):
    """Плееры фильмов и сериалов («балансеры»), которые стоят на kinogo и похожих сайтах. Каждый отдаёт сезоны и
    серии со ссылками на HLS (или mp4), ссылки живут недолго — поэтому сериал это плейлист из записей «этот же плеер
    + #season=N&episode=M», а поток серии ищется уже при её скачивании.
    Выбор — во фрагменте адреса: #season=2 — весь сезон, #season=2&episode=5 — одна серия.
    Без Referer сайта, на котором стоит плеер, они отдают заглушку или 404 — GenericIE передаёт его (smuggle_url).
    В info кладём udl_choices (сезоны, что выбрано) — по ним страница программы рисует выбор сезона и серии.
    Подкласс даёт _show(адрес, referer) → {id, title, thumbnail, movie: поток | seasons: {сезон: {серия: поток}}},
    поток — {m3u8 | links: [(подпись, адрес)], duration, thumbnail, title}."""
    _VALID_URL = False  # сам по себе не разборщик

    def _show(self, url, referer):
        raise NotImplementedError

    def _default_referer(self, url):
        return None

    def _real_extract(self, url):
        url, data = unsmuggle_url(url, {})
        base, _, frag = url.partition('#')
        pick = {k: int(v[-1]) for k, v in parse_qs(frag).items() if k in ('season', 'episode') and v[-1].isdigit()}
        show = self._show(base, data.get('referer') or self._default_referer(base))
        season, episode = pick.get('season'), pick.get('episode')
        choices = {'base': base, 'season': season, 'episode': episode}
        common = {'thumbnail': show.get('thumbnail'), 'udl_choices': choices, 'extractor_key': self.IE_NAME}
        seasons = show.get('seasons')
        if not seasons:
            return {**common, **self._stream(show['movie'], str(show['id']), base), 'title': show['title']}
        choices['seasons'] = [{'season': s, 'episodes': sorted(eps)} for s, eps in sorted(seasons.items())]
        if season is not None and season not in seasons:
            raise ExtractorError(tr('В плеере {} нет {} сезона', 'Player {} has no season {}').format(self.IE_NAME, season), expected=True)
        if season is not None and episode is not None:
            ep = seasons[season].get(episode)
            if not ep:
                raise ExtractorError(tr('В плеере {} нет {} сезона, {} серии', 'Player {} has no season {}, episode {}')
                                     .format(self.IE_NAME, season, episode), expected=True)
            return {
                **common, **self._stream(ep, f'{show["id"]}-s{season}e{episode}', base),
                'title': self._episode_title(show, season, episode, ep),
                'thumbnail': ep.get('thumbnail') or show.get('thumbnail'),
                'series': show['title'], 'season_number': season, 'episode_number': episode,
            }
        entries = [
            self.url_result(self._with_data(f'{base}#season={s}&episode={e}', data), self.ie_key(),
                            f'{show["id"]}-s{s}e{e}', self._episode_title(show, s, e, ep),
                            duration=ep.get('duration'), thumbnail=ep.get('thumbnail'))
            for s, eps in sorted(seasons.items()) if season in (None, s) for e, ep in sorted(eps.items())]
        title = show['title'] + (tr(' — {} сезон', ' — season {}').format(season) if season is not None else '')
        playlist_id = f'{show["id"]}' + (f'-s{season}' if season is not None else '')
        return self.playlist_result(entries, playlist_id, title, **common)

    @staticmethod
    def _with_data(url, data):
        return smuggle_url(url, data) if data else url

    @staticmethod
    def _episode_title(show, season, episode, ep):
        name = ep.get('title')
        return (show['title'] + tr(' — {} сезон, {} серия', ' — season {}, episode {}').format(season, episode)
                + (f'. {name}' if name else ''))

    def _m3u8(self, url, video_id, headers):
        """HLS + язык субтитров по названию: LANGUAGE у них нет (NAME="ENG #01"), yt-dlp называл их «und»."""
        res = self._download_webpage_handle(url, video_id, 'Downloading m3u8', headers=headers, fatal=False)
        if not res:
            return [], {}
        doc, urlh = res
        formats, subtitles = self._parse_m3u8_formats_and_subtitles(
            doc, urlh.url, 'mp4', m3u8_id='hls', video_id=video_id, headers=headers, fatal=False)
        names = re.findall(r'#EXT-X-MEDIA:(?=[^\n]*TYPE=SUBTITLES)(?![^\n]*LANGUAGE=)[^\n]*NAME="([^"]*)"', doc)
        unknown = subtitles.pop('und', [])
        if len(names) != len(unknown):
            names = [''] * len(unknown)
        for name, sub in zip(names, unknown):
            lang = next((lang for lang, rx in TRACK_LANGS if re.search(rx, name, re.I)), 'und')
            subtitles.setdefault(lang, []).append(sub)
        return formats, subtitles

    def _stream(self, stream, video_id, base):
        headers = {'Referer': urljoin(base, '/')}  # CDN плееров смотрит, откуда запрос
        formats, subtitles = [], {}
        # alts — запасные источники (озвучки ShizaHD): берём первый, который отвечает
        for alt in stream.get('alts') or [stream]:
            links = [('', alt['m3u8'])] if alt.get('m3u8') else alt.get('links') or []
            master = next((link for _, link in links if '.m3u8' in link), None)
            if master:  # первый m3u8 («Auto») — общий, со всеми качествами и дорожками
                formats, subtitles = self._m3u8(master, video_id, headers)
            elif links and self._request_webpage(
                    Request(links[0][1], headers={**headers, 'Range': 'bytes=0-0'}), video_id,
                    'Checking video link', fatal=False):
                for label, link in links:
                    height = int_or_none(self._search_regex(r'(\d{3,4})p', label, 'height', default=None))
                    # прямые mp4 (у ShizaHD — с CDN ok.ru): h264 + AAC, иначе программа не покажет качества
                    formats.append({'url': link, 'format_id': f'{height}p' if height else label or None,
                                    'height': height, 'ext': 'mp4', 'vcodec': 'avc1', 'acodec': 'mp4a'})
            if formats:
                break
        if not formats:
            raise ExtractorError(tr('Плеер {} не отдал видео', 'Player {} did not give the video').format(self.IE_NAME), expected=True)
        label_tracks(formats)
        return {'id': video_id, 'formats': formats, 'subtitles': subtitles, 'http_headers': headers,
                'duration': int_or_none(stream.get('duration'))}


def _number(text):
    m = re.search(r'\d+', str(text or ''))
    return int(m[0]) if m else None


class CCDNIE(BalancerIE):
    """CCDN (api.namy.ws, api.nextembed.ws…): всё в makePlayer({…}) на странице плеера — у фильма source.hls,
    у сериала playlist.seasons[{season, episodes[{episode, hls, duration, poster}]}]."""
    IE_NAME = 'CCDN'
    _VALID_URL = r'https?://(?:[\w-]+\.)*[\w-]+\.ws/embed/(?:movie|serial|kp|imdb)/(?P<id>\d+)'

    def _default_referer(self, url):
        host = traverse_obj(parse_qs(urlparse(url).query), ('host', -1))
        return f'https://{host}/' if host else None

    def _show(self, url, referer):
        video_id = self._match_id(url)
        webpage = self._download_webpage(url, video_id, headers={'Referer': referer} if referer else {})
        start = webpage.find('makePlayer({')
        if start < 0:
            raise ExtractorError(tr('Плеер CCDN не открылся (видео удалено или закрыто для этого сайта)',
                                    'The CCDN player did not open (the video was deleted or is closed for this site)'), expected=True)
        player = webpage[start:]
        poster = url_or_none(self._search_regex(r'\bposter:\s*"([^"]+)"', player, 'poster', default=None))
        at = player.find('seasons:')
        if at < 0:
            m3u8 = self._search_regex(r'\bhls:\s*"([^"]+)"', player, 'hls')
            title = self._search_regex(r'\btitle:\s*"([^"]+)"', player, 'title', default=None) \
                or self._html_extract_title(webpage)
            return {'id': video_id, 'title': title, 'thumbnail': poster, 'movie': {'m3u8': m3u8}}
        raw = json.JSONDecoder().raw_decode(player[at + 8:].lstrip())[0]
        seasons = {}
        for season in raw:
            if season.get('blocked') or _number(season.get('season')) is None:
                continue
            for ep in season.get('episodes') or []:
                if _number(ep.get('episode')) is not None and url_or_none(ep.get('hls')):
                    seasons.setdefault(_number(season['season']), {})[_number(ep['episode'])] = {
                        'm3u8': ep['hls'], 'duration': ep.get('duration'), 'thumbnail': url_or_none(ep.get('poster'))}
        # <title>Менталист (1 сезон) - 1 серия</title>
        title = re.sub(r'\s*\(\d+\s*сезон\).*', '', self._html_extract_title(webpage) or '') or video_id
        return {'id': video_id, 'title': title, 'thumbnail': poster, 'seasons': seasons}


class ShizaHDIE(BalancerIE):
    """ShizaHD: window.SHZ на странице плеера, список — SHZ.file (list.txt, PlayerJS): у сериала
    [{title: Сезон 1, folder: [{title: 1 серия, folder: [озвучки {title, file}]}]}], у фильма — сразу озвучки.
    file зашифрован (playerjs_decode); в нём «[Auto]…m3u8,[1080p]…» или прямые mp4 по качествам."""
    IE_NAME = 'ShizaHD'
    _VALID_URL = r'https?://(?:[\w-]+\.)*shizahd\.[a-z]+/iframe/(?P<id>[0-9a-f]+)'

    @staticmethod
    def _voice(voices):
        """Озвучки по порядку плеера (первая обычно русская) — запасные друг для друга: у фильмов первая бывает
        ссылкой на свой прокси src.mp4, который отвечает 502."""
        alts = [{'links': playerjs_links(playerjs_decode(v['file']))}
                for v in voices or [] if isinstance(v, dict) and v.get('file')]
        return {'alts': alts} if alts else None

    def _show(self, url, referer):
        video_id = self._match_id(url)
        webpage = self._download_webpage(url, video_id, headers={'Referer': referer} if referer else {})
        shz = self._search_json(r'window\.SHZ\s*=', webpage, 'SHZ', video_id, transform_source=js_to_json)
        listing = self._download_json(urljoin(url, shz['file']), video_id, 'Downloading playlist',
                                      headers={'Referer': url})
        show = {'id': video_id, 'title': shz.get('title') or video_id,
                'thumbnail': url_or_none(urljoin(url, shz.get('poster') or ''))}
        if not any(isinstance(x, dict) and x.get('folder') for x in listing or []):
            show['movie'] = self._voice(listing)
            if not show['movie']:
                raise ExtractorError(tr('Плеер ShizaHD не отдал видео', 'The ShizaHD player did not give the video'), expected=True)
            return show
        show['seasons'] = {}
        for season in listing:
            for ep in season.get('folder') or []:
                stream = self._voice(ep.get('folder'))
                if stream and _number(season.get('title')) is not None and _number(ep.get('title')) is not None:
                    show['seasons'].setdefault(_number(season['title']), {})[_number(ep['title'])] = stream
        return show


class VeoVeoIE(BalancerIE):
    """VeoVeo (адрес плеера …/balancer-api/iframe?movie_id=…&token=…, домены меняются): SPA, данные — REST
    <хост>/balancer-api/proxy/playlists/catalog-api с заголовком DLE-API-TOKEN (= token из адреса):
    contents/<id> (название, постер, hasMultipleEpisodes) и episodes?content-id=<id> (серии с season.order, order,
    m3u8MasterFilePath или episodeVariants[{filepath, duration}]; у фильма одна «серия»)."""
    IE_NAME = 'VeoVeo'
    _VALID_URL = r'https?://[\w.-]+/balancer-api/iframe\?(?:[^#]*&)?movie_id=(?P<id>\d+)'

    def _show(self, url, referer):
        video_id = self._match_id(url)
        token = traverse_obj(parse_qs(urlparse(url).query), ('token', -1))
        api = urljoin(url, '/balancer-api/proxy/playlists/catalog-api/')
        headers = {'DLE-API-TOKEN': token or '', 'Referer': url}
        content = self._download_json(f'{api}contents/{video_id}', video_id, headers=headers)
        episodes = self._download_json(f'{api}episodes', video_id, 'Downloading episodes',
                                       query={'content-id': video_id}, headers=headers)
        streams = []
        for ep in episodes or []:
            variant = traverse_obj(ep, ('episodeVariants', lambda _, v: url_or_none(v['filepath'])), get_all=False) or {}
            m3u8 = url_or_none(ep.get('m3u8MasterFilePath')) or variant.get('filepath')
            if m3u8:
                streams.append((ep, {'m3u8': m3u8, 'duration': variant.get('duration'), 'title': ep.get('title'),
                                     'thumbnail': url_or_none(variant.get('previewImageFilepath'))}))
        if not streams:
            raise ExtractorError(tr('Плеер VeoVeo не отдал видео', 'The VeoVeo player did not give the video'), expected=True)
        show = {'id': video_id, 'title': content.get('title') or content.get('originalTitle') or video_id,
                'thumbnail': url_or_none(content.get('posterUrl'))}
        if not content.get('hasMultipleEpisodes'):
            show['movie'] = streams[0][1]
            return show
        show['seasons'] = {}
        for ep, stream in streams:
            s, e = int_or_none(traverse_obj(ep, ('season', 'order'))), int_or_none(ep.get('order'))
            if s is not None and e is not None:
                show['seasons'].setdefault(s, {})[e] = stream
        return show


BALANCERS = (CCDNIE, ShizaHDIE, VeoVeoIE)
PLAYER_ATTRS = ('data-src', 'data-iframe', 'data-url', 'data-link', 'data-player', 'src')


def player_list(page_url, webpage):
    """Плееры-балансеры на странице: кнопки вида <li data-src="…">CCDN</li> (kinogo — iframe без src, адрес ставит
    JS по щелчку) или iframe. → [{name, url, ie}] в порядке страницы, без повторов."""
    found, seen = [], set()
    for m in re.finditer(r'<(?P<tag>[a-z]+)\b(?P<attrs>[^>]*)>(?P<text>[^<]{0,60})', webpage, re.I):
        if 'data-' not in m['attrs'] and m['tag'].lower() != 'iframe':
            continue
        attrs = extract_attributes(f'<x{m["attrs"]}>')
        for key in PLAYER_ATTRS:
            src = url_or_none(urljoin(page_url, attrs.get(key) or ''))
            ie = src and next((ie for ie in BALANCERS if ie.suitable(src)), None)
            if ie:
                if src not in seen:
                    seen.add(src)
                    found.append({'name': clean_html(m['text']) or ie.IE_NAME, 'url': src, 'ie': ie.ie_key()})
                break
    return found


# iframe с рекламой, счётчиками, комментариями — не плееры
IFRAME_SKIP = re.compile(r'(?i)(?:\b|_)(?:ads?|adv|banners?|exoclick|juicyads|trafficjunky|tsyndicate|popunder|counter'
                         r'|metrika|analytics|googletagmanager|doubleclick|recaptcha|captcha|disqus|comments?|chat'
                         r'|facebook\.com/plugins|vk\.com/widget|twitter\.com/widgets)(?:\b|_)')
IFRAME_PLAYER = re.compile(r'(?i)/(?:embed|player|iframe|e|v|video|videos|play|watch)(?:[/._?=-]|$)')


def player_iframe(page_url, webpage):
    """Адрес встроенного плеера (iframe) на странице или None. Подходят iframe с «во весь экран»
    или с /embed/, /player/ в адресе, лучше — с тем и другим; крошечные (1×1) и рекламные пропускаем."""
    found = []
    for tag in re.findall(r'<iframe\b[^>]*>', webpage, re.I):
        attrs = extract_attributes(tag)
        src = url_or_none(urljoin(page_url, unescapeHTML(attrs.get('src') or attrs.get('data-src') or '')))
        if not src or src.split('#')[0] == page_url.split('#')[0] or IFRAME_SKIP.search(src):
            continue
        if any(int_or_none(attrs.get(k)) in (0, 1) for k in ('width', 'height')):
            continue
        rank = ('allowfullscreen' in tag.lower()) + bool(IFRAME_PLAYER.search(src.split('?')[0]))
        if rank:
            found.append((-rank, len(found), src))
    return min(found)[2] if found else None


class GenericIE(_GenericIE):
    """Общий разборщик + вход во встроенный плеер с другого сайта (iframe).

    Пример: sexlends.org — на странице только <iframe src="https://vwfastdelivery.com/embed/…">, а внутри
    KVS-плеер (kt_player), который общий разборщик понимает, если открыть сам iframe. В iframe идём,
    только если на странице ничего не нашлось, и только на один уровень вглубь; Referer — адрес
    страницы (без него такие плееры часто не отдают видео)."""

    def _real_extract(self, url):
        self._udl_nested = bool(unsmuggle_url(url, {})[1].get('udl_iframe'))
        return super()._real_extract(url)

    def _extract_kvs(self, url, webpage, video_id):
        info = super()._extract_kvs(url, webpage, video_id)
        # у плеера для встраивания заголовок «Название / Embed Player», а само название — в flashvars
        title = self._search_regex(r"\bvideo_title\s*:\s*'([^']+)'", webpage, 'title', default=None)
        if title:
            info['title'] = unescapeHTML(title)
        return info

    def _extract_embeds(self, url, webpage, *, urlh=None, info_dict={}):
        page_url = urlh.url if urlh else unsmuggle_url(url)[0]
        # плееры фильмов и сериалов — главное на странице, раньше трейлеров с YouTube и прочего
        players = [] if getattr(self, '_udl_nested', False) else player_list(page_url, webpage)
        if players:
            return [self._extract_balancer(url, page_url, players)]
        embeds = list(super()._extract_embeds(url, webpage, urlh=urlh, info_dict=info_dict))
        if embeds or getattr(self, '_udl_nested', False):
            return embeds
        frame = player_iframe(page_url, webpage)
        if not frame:
            return []
        self._downloader.write_debug(f'Player iframe: {frame}')
        if any(ie.suitable(frame) for ie in gen_extractor_classes() if ie.ie_key() != 'Generic'):
            return [self.url_result(frame)]  # у сайта плеера есть свой разборщик
        return [self.url_result(smuggle_url(frame, {'referer': page_url, 'udl_iframe': True}), 'Generic')]

    def _extract_balancer(self, url, page_url, players):
        """Страница с несколькими плеерами (kinogo: CCDN, ShizaHD, VeoVeo…). Плеер — #player=N во фрагменте адреса
        (с 1, по порядку на странице), туда же #season и #episode — их передаём плееру. Без выбора — первый, который
        открылся. Разбираем плеер здесь же, а не url_result: так в info остаются udl_choices со списком плееров."""
        base, _, frag = unsmuggle_url(url)[0].partition('#')
        pick = parse_qs(frag)
        chosen = int_or_none(traverse_obj(pick, ('player', -1)))
        rest = '&'.join(f'{k}={pick[k][-1]}' for k in ('season', 'episode') if k in pick)
        order = [chosen] if chosen and 1 <= chosen <= len(players) else range(1, len(players) + 1)
        error = None
        for n in order:
            player = players[n - 1]
            target = smuggle_url(player['url'] + (f'#{rest}' if rest else ''), {'referer': page_url})
            try:
                info = self._downloader.get_info_extractor(player['ie']).extract(target)
            except ExtractorError as e:
                error = e
                self.report_warning(f'Плеер {player["name"]}: {e.orig_msg}')
                continue
            info.setdefault('udl_choices', {}).update(base=base, player=n, players=[p['name'] for p in players])
            info['extractor_key'] = player['name']
            return info
        raise error


OURS = {'YapFilesIE': YapFilesIE, 'YaplakalIE': YaplakalIE, 'OdnoklassnikiIE': OdnoklassnikiIE, 'ComdiIE': ComdiIE,
        'InstagramIE': InstagramIE, 'OsnovaIE': OsnovaIE, 'D3IE': D3IE,
        **{ie.__name__: ie for ie in BALANCERS}}


def register():
    """Ставит свои разборщики первыми (одноимённые — вместо встроенных)."""
    import_extractors()
    ies = _extractors.value
    rest = {k: v for k, v in ies.items() if k not in OURS}
    rest['GenericIE'] = GenericIE  # общий остаётся на своём месте, последним: он берёт всё, что не взяли другие
    ies.clear()
    ies.update(OURS)
    ies.update(rest)
