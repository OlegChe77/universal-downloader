// Язык страницы. <html lang> ставит сервер по настройке «lang» (settings.json; пусто — как язык Windows).
// Русский — в разметке и в коде как есть; английский:
// - L('русский', 'English') — строки в коде;
// - data-en="…" у элемента — его innerHTML по-английски (абзацы со ссылками и <b>);
// - EN_TEXT — короткие тексты разметки и атрибуты title/placeholder/aria-label (ключ — русский текст без лишних пробелов).
const EN = document.documentElement.lang === 'en';
const L = (ru, en) => EN ? en : ru;

const EN_TEXT = {
  'Универсальный скачиватель': 'Universal Downloader',
  'Видео и музыка с YouTube, VK, Rutube, SoundCloud и ещё 1000+ сайтов — прямо на компьютер':
    'Video and music from YouTube, SoundCloud, Vimeo, TikTok and 1000+ other sites — straight to your computer',
  'Поддержать разработку программы (DonationAlerts)': 'Support the development of the program (DonationAlerts)',
  'Помощь проекту': 'Support the project',
  'Сменить тему оформления': 'Change the color theme',
  'Тема: как в Windows': 'Theme: as in Windows',
  'Как это работает и что делать, если не качает': 'How it works and what to do if downloads fail',
  'Помощь': 'Help',
  'Скачивать прямо с сайта': 'Download right from a site',
  '— расширение и кнопка-закладка для всех браузеров': '— an extension and a bookmark button for all browsers',
  'Яндекс Браузер': 'Yandex Browser',
  'Опера': 'Opera',
  'Расширение': 'Extension',
  'Скачать расширение (zip)': 'Download the extension (zip)',
  'Подробная инструкция': 'Detailed instructions',
  'Кнопка-закладка': 'Bookmark button',
  'Скачать в программе': 'Send to Downloader',
  'Что скачиваем': 'What to download',
  'По ссылке': 'By link',
  'видео, звук, плейлист, список': 'video, audio, playlist, list',
  'Музыка': 'Music',
  'поиск песни по названию': 'find a song by name',
  'VPN': 'Proxy',
  'закрытые сайты, прокси': 'VPN app, blocked sites',
  'Вставьте ссылку, например https://youtu.be/…': 'Paste a link, for example https://youtu.be/…',
  'Ссылка на видео или страницу': 'Link to a video or page',
  'Найти': 'Find',
  'Очистить поле и убрать найденное': 'Clear the field and the result',
  'Очистить поле и убрать список': 'Clear the field and the list',
  'Очистить': 'Clear',
  'Сохранять в:': 'Save to:',
  'Выбрать папку': 'Choose folder',
  'Открыть': 'Open',
  'Перед каждой загрузкой откроется окно выбора папки': 'A folder picker opens before each download',
  'спрашивать папку каждый раз': 'ask for the folder every time',
  'Скопируйте адрес из браузера и вставьте сюда — варианты появятся сами': 'Copy the address from the browser and paste it here — the options appear by themselves',
  'Несколько ссылок — каждую с новой строки, скачаются по очереди': 'Several links — one per line, they download one after another',
  'Смотрю, что лежит по ссылке…': 'Checking what is at the link…',
  'Что скачать': 'What to download',
  'Плеер': 'Player',
  'Сезон': 'Season',
  'Серия': 'Episode',
  'Звуковая дорожка': 'Audio track',
  'Субтитры': 'Subtitles',
  'Язык субтитров': 'Subtitle language',
  'Как сохранить субтитры': 'How to save subtitles',
  'вшить в MP4': 'embed in MP4',
  'отдельным файлом .srt': 'separate .srt file',
  'Видео со звуком': 'Video with audio',
  'Как склеить короткое видео с длинным звуком': 'How to join a short video with long audio',
  'по кругу, пока играет звук': 'loop while the audio plays',
  'один раз, звук обрезать': 'play once, cut the audio',
  'Только звук': 'Audio only',
  'Список ссылок': 'List of links',
  'Выберите один вариант для всех — файлы скачаются по очереди, по одному.': 'Choose one option for all — the files download one at a time.',
  'Исполнитель и название песни': 'Artist and song title',
  'Искать': 'Search',
  'Ищем сразу на YouTube, SoundCloud и Rutube и показываем по 10 вариантов': 'We search YouTube, SoundCloud and Rutube at once and show 10 results at a time',
  'Например:': 'For example:',
  'Кино — Группа крови': 'Daft Punk — Get Lucky',
  'Земфира — Хочешь': 'Adele — Hello',
  'Выбрать все': 'Select all',
  'Скачать MP3': 'Download MP3',
  'Показать ещё 10': 'Show 10 more',
  'Больше ничего не нашлось — попробуйте написать запрос иначе.': 'Nothing more found — try wording the query differently.',
  'Сейчас:': 'Now:',
  'адрес прокси, например socks5://127.0.0.1:10808': 'proxy address, for example socks5://127.0.0.1:10808',
  'Адрес прокси': 'Proxy address',
  'Сохранить': 'Save',
  'все сайты через прокси': 'all sites through the proxy',
  'Защищённый DNS': 'Secure DNS',
  'Все варианты скачивания': 'All download options',
  'Закрыть': 'Close',
  'Загружаю варианты…': 'Loading options…',
  'Прослушивание': 'Listening',
  'Предыдущая (или сначала)': 'Previous (or from the start)',
  'Следующая': 'Next',
  'Перемотка': 'Seek',
  'Выключить звук': 'Mute',
  'Громкость': 'Volume',
  'Скачать эту песню в MP3': 'Download this song as MP3',
  'Закрыть плеер': 'Close the player',
  // help.html и quick.js
  'Перетащите кнопку мышкой на панель закладок под адресной строкой': 'Drag the button with the mouse to the bookmarks bar under the address bar',
};

const EN_ATTRS = ['title', 'placeholder', 'aria-label'];

function translatePage(root = document.body) {
  if (!EN) return;
  const key = s => s.replace(/\s+/g, ' ').trim();
  for (const el of root.querySelectorAll('[data-en]')) el.innerHTML = el.dataset.en;
  for (const el of root.querySelectorAll('[title], [placeholder], [aria-label]')) {
    for (const a of EN_ATTRS) {
      const v = el.getAttribute(a);
      if (v && EN_TEXT[key(v)]) el.setAttribute(a, EN_TEXT[key(v)]);
    }
  }
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: n => n.parentElement.closest('script, style') ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
  });
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const k = key(n.nodeValue);
    if (k && EN_TEXT[k]) n.nodeValue = n.nodeValue.match(/^\s*/)[0] + EN_TEXT[k] + n.nodeValue.match(/\s*$/)[0];
  }
}
