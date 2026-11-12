// Кнопка на панели и пункт меню «Скачать в программе»: открывают страницу программы с нужной ссылкой.
// Порт — в config.js (программа подставляет свой, когда отдаёт архив).
// Chrome/Edge/Яндекс запускают service worker — config.js подключаем сами. Firefox берёт background.scripts
// из manifest.json: config.js там уже загружен первым, а importScripts нет.
if (typeof importScripts === 'function') importScripts('config.js');

const BASE = `http://127.0.0.1:${self.UDL_PORT}/`;

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    const en = self.UDL_LANG === 'en';  // язык программы, когда она отдала архив (config.js)
    chrome.contextMenus.create({id: 'udl-page', title: en ? 'Send to Downloader' : 'Скачать в программе',
      contexts: ['page', 'frame', 'video', 'audio', 'image', 'selection']});
    chrome.contextMenus.create({id: 'udl-link', title: en ? 'Send to Downloader (this link)' : 'Скачать в программе (эту ссылку)',
      contexts: ['link']});
  });
});

const isWeb = u => /^https?:\/\//i.test(u || '');

// Запущена ли программа: отвечает ли её сервер (любой ответ, лишь бы соединение было).
async function running() {
  try {
    await fetch(BASE + 'favicon.ico', {method: 'HEAD', cache: 'no-store'});
    return true;
  } catch {
    return false;
  }
}

// Если вкладка программы уже открыта — переходим в неё: меняется только #url=…, страница не
// перезагружается (плеер продолжает играть) и сама разбирает ссылку. Иначе — новая вкладка рядом.
// Программа не запущена — вкладка launch.html: она запускает программу и переходит в неё, когда та заработает.
async function send(link, from) {
  if (!isWeb(link) || link.startsWith(BASE)) link = '';  // chrome://, сама программа — просто открываем её
  if (!(await running())) {
    const opts = {url: chrome.runtime.getURL('launch.html') + (link ? '#' + encodeURIComponent(link) : '')};
    if (from && from.index >= 0) { opts.index = from.index + 1; opts.windowId = from.windowId; }
    return (await chrome.tabs.create(opts)).id;
  }
  const tabs = await chrome.tabs.query({url: 'http://127.0.0.1/*'});
  const tab = tabs.find(t => {
    try { const u = new URL(t.url); return u.port === String(self.UDL_PORT) && u.pathname === '/'; } catch { return false; }
  });
  const q = link ? encodeURIComponent(link) : '';
  if (tab) {
    await chrome.tabs.update(tab.id, {url: link ? `${BASE}#url=${q}` : BASE, active: true});
    await chrome.windows.update(tab.windowId, {focused: true});
    return tab.id;
  }
  const opts = {url: link ? `${BASE}?url=${q}` : BASE};
  if (from && from.index >= 0) { opts.index = from.index + 1; opts.windowId = from.windowId; }
  return (await chrome.tabs.create(opts)).id;
}

// Урок GetCourse открывается только после входа — программа его не откроет. Но видео в нём — в плеере
// (…/sign-player/?json=…), который входа не требует: берём ссылки на плеер со страницы и дописываем название
// урока (#udl-title=…, его читает sites.py). Тот же поиск — в кнопке-закладке (web/quick.js).
const PLAYER_RE = /\/sign-player\/?\?[^#]*json=/;

// Выполняется на самой странице: только то, что внутри функции.
function findPlayers() {
  const re = /https?:\/\/[\w.-]+\/sign-player\/?\?[^"'\s<>]*?json=[^"'\s<>]+/g;
  const text = document.documentElement.outerHTML.replace(/\\\//g, '/').replace(/&amp;/g, '&') + ' ' +
    performance.getEntriesByType('resource').map(e => e.name).join(' ');
  const links = [...new Set(text.match(re) || [])];
  const title = document.title.trim();
  return links.map((u, i) => title ? `${u}#udl-title=${encodeURIComponent(links.length > 1 ? `${title} (${i + 1})` : title)}` : u);
}

// Вкладка, открытая нажатием на кнопку или пункт меню, доступна расширению (activeTab). Не вышло
// (chrome://, магазин расширений) — пустой список, шлём адрес страницы как раньше.
async function players(tab) {
  if (!tab || !isWeb(tab.url) || tab.url.startsWith(BASE)) return [];
  try {
    const [res] = await chrome.scripting.executeScript({target: {tabId: tab.id}, func: findPlayers});
    return (res && res.result) || [];
  } catch {
    return [];
  }
}

async function onAction(tab) {
  const found = await players(tab);
  return send(found.length ? found.join('\n') : tab.url, tab);
}

async function onMenu(info, tab) {
  if (info.menuItemId === 'udl-link') return send(info.linkUrl || (tab && tab.url), tab);
  // во встроенном видео (iframe) берём адрес самого видео, а не страницы вокруг
  if (info.frameUrl && PLAYER_RE.test(info.frameUrl)) {
    const title = ((tab && tab.title) || '').trim();
    return send(title ? `${info.frameUrl.split('#')[0]}#udl-title=${encodeURIComponent(title)}` : info.frameUrl, tab);
  }
  if (info.frameUrl) return send(info.frameUrl, tab);
  const found = await players(tab);
  return send(found.length ? found.join('\n') : info.pageUrl || (tab && tab.url), tab);
}

chrome.action.onClicked.addListener(onAction);
chrome.contextMenus.onClicked.addListener(onMenu);
