"""Свои разборщики сайтов для yt-dlp. register() ставит их перед встроенными.

- ЯП файлы (yapfiles.ru): встроенный YapFilesIE помечен сломанным — берёт устаревший md5 (403)
  и не знает страниц /show/. md5 у каждого ролика свой, он есть на странице плеера get_player.
- ЯПлакалъ (yaplakal.com): плеер в теме — <div id="yfp-<пост>-<id>" rel="yapfiles">, iframe
  рисует JS, поэтому общий разборщик его не видит и берёт мусорный <audio> (→ HTTP 404).
- Одноклассники (ok.ru): встроенный падает на новом формате плеера (см. OdnoklassnikiIE).
- Встроенный плеер с другого сайта (iframe): общий разборщик заходит только в плееры известных
  сайтов. Если на странице больше ничего нет — идём в iframe сами (см. GenericIE). Так берутся
  KVS-плееры (kvsplayer.com, на многих сайтах «для взрослых») и другие плееры с чужих доменов.
"""
import base64
import re
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


OURS = {'YapFilesIE': YapFilesIE, 'YaplakalIE': YaplakalIE, 'OdnoklassnikiIE': OdnoklassnikiIE}


def register():
    """Ставит свои разборщики первыми (одноимённые — вместо встроенных)."""
    import_extractors()
    ies = _extractors.value
    rest = {k: v for k, v in ies.items() if k not in OURS}
    rest['GenericIE'] = GenericIE  # общий остаётся на своём месте, последним: он берёт всё, что не взяли другие
    ies.clear()
    ies.update(OURS)
    ies.update(rest)
