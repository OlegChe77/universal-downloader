"""Свои разборщики сайтов для yt-dlp. register() ставит их перед встроенными.

- ЯП файлы (yapfiles.ru): встроенный YapFilesIE помечен сломанным — берёт устаревший md5 (403)
  и не знает страниц /show/. md5 у каждого ролика свой, он есть на странице плеера get_player.
- ЯПлакалъ (yaplakal.com): плеер в теме — <div id="yfp-<пост>-<id>" rel="yapfiles">, iframe
  рисует JS, поэтому общий разборщик его не видит и берёт мусорный <audio> (→ HTTP 404).
- Одноклассники (ok.ru): встроенный падает на новом формате плеера (см. OdnoklassnikiIE).
- COMDI (comdi.com): эфиры и их записи, плеер встраивается на сайты скриптом embed-player.js
  с data-watchKey — без своего разборщика «Unsupported URL» (см. ComdiIE).
- Встроенный плеер с другого сайта (iframe): общий разборщик заходит только в плееры известных
  сайтов. Если на странице больше ничего нет — идём в iframe сами (см. GenericIE). Так берутся
  KVS-плееры (kvsplayer.com, на многих сайтах «для взрослых») и другие плееры с чужих доменов.
"""
import base64
import re
import time
from urllib.parse import urljoin

from yt_dlp.extractor import gen_extractor_classes, import_extractors
from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.extractor.generic import GenericIE as _GenericIE
from yt_dlp.extractor.odnoklassniki import OdnoklassnikiIE as _OdnoklassnikiIE
from yt_dlp.globals import extractors as _extractors
from yt_dlp.utils import (ExtractorError, extract_attributes, int_or_none, smuggle_url, unescapeHTML,
                          unsmuggle_url, url_or_none)

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
            raise ExtractorError('Видео удалено с ЯП файлов', expected=True)

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
            raise ExtractorError('ЯП файлы не отдали ссылку на видео', expected=True)
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
            raise ExtractorError('В этой теме ЯПлакалъ нет видео', expected=True)
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
                raise ExtractorError('Эфир COMDI открыт только после регистрации — записи без входа нет',
                                     expected=True)
            start = int_or_none(event.get('timeStart'))
            if start and start > now:
                raise ExtractorError('Эфир COMDI ещё не начался', expected=True)
            raise ExtractorError('У эфира COMDI нет записи', expected=True)
        if len(entries) == 1:
            return {**entries[0], 'id': key, 'webpage_url': url}
        return self.playlist_result(entries, key, title)


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
        embeds = list(super()._extract_embeds(url, webpage, urlh=urlh, info_dict=info_dict))
        if embeds or getattr(self, '_udl_nested', False):
            return embeds
        page_url = urlh.url if urlh else unsmuggle_url(url)[0]
        frame = player_iframe(page_url, webpage)
        if not frame:
            return []
        self._downloader.write_debug(f'Player iframe: {frame}')
        if any(ie.suitable(frame) for ie in gen_extractor_classes() if ie.ie_key() != 'Generic'):
            return [self.url_result(frame)]  # у сайта плеера есть свой разборщик
        return [self.url_result(smuggle_url(frame, {'referer': page_url, 'udl_iframe': True}), 'Generic')]


OURS = {'YapFilesIE': YapFilesIE, 'YaplakalIE': YaplakalIE, 'OdnoklassnikiIE': OdnoklassnikiIE, 'ComdiIE': ComdiIE}


def register():
    """Ставит свои разборщики первыми (одноимённые — вместо встроенных)."""
    import_extractors()
    ies = _extractors.value
    rest = {k: v for k, v in ies.items() if k not in OURS}
    rest['GenericIE'] = GenericIE  # общий остаётся на своём месте, последним: он берёт всё, что не взяли другие
    ies.clear()
    ies.update(OURS)
    ies.update(rest)
