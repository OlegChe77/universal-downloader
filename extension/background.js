// Кнопка на панели и пункт меню «Скачать в программе»: открывают страницу программы с нужной ссылкой.
// Порт — в config.js (программа подставляет свой, когда отдаёт архив).
// Chrome/Edge/Яндекс запускают service worker — config.js подключаем сами. Firefox берёт background.scripts
// из manifest.json: config.js там уже загружен первым, а importScripts нет.
if (typeof importScripts === 'function') importScripts('config.js');

const BASE = `http://127.0.0.1:${self.UDL_PORT}/`;

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({id: 'udl-page', title: 'Скачать в программе', contexts: ['page', 'frame', 'video', 'audio', 'image', 'selection']});
    chrome.contextMenus.create({id: 'udl-link', title: 'Скачать в программе (эту ссылку)', contexts: ['link']});
  });
});

const isWeb = u => /^https?:\/\//i.test(u || '');

// Если вкладка программы уже открыта — переходим в неё: меняется только #url=…, страница не
// перезагружается (плеер продолжает играть) и сама разбирает ссылку. Иначе — новая вкладка рядом.
async function send(link, from) {
  if (!isWeb(link) || link.startsWith(BASE)) link = '';  // chrome://, сама программа — просто открываем её
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

function onAction(tab) { return send(tab.url, tab); }

function onMenu(info, tab) {
  // во встроенном видео (iframe) берём адрес самого видео, а не страницы вокруг
  const link = info.menuItemId === 'udl-link' ? info.linkUrl : info.frameUrl || info.pageUrl;
  return send(link || (tab && tab.url), tab);
}

chrome.action.onClicked.addListener(onAction);
chrome.contextMenus.onClicked.addListener(onMenu);
