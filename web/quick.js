// Букмарклет «Скачать в программе» — общий для главной и help.html.
// a[data-bookmarklet] получает код, [data-drag-hint] — картинку-подсказку, [data-copy-bookmarklet] копирует код.
(() => {
  const BASE = location.origin + '/';
  const NAME = 'udl-downloader';  // имя вкладки программы: повторное нажатие попадает в неё, если браузер её находит

  // Открывает программу с адресом текущей страницы. Через #url=… — тогда уже открытая вкладка программы
  // не перезагружается, а разбирает ссылку. Всплывающее окно запрещено — переходим в этой же вкладке.
  const code = `(function(){var o='${BASE}',u=location.href;if(u.indexOf(o)==0)return;if(window.name=='${NAME}')window.name='';` +
    `var w=window.open(o+'#url='+encodeURIComponent(u),'${NAME}');if(!w)location.href=o+'?url='+encodeURIComponent(u)})()`;
  // '#' в адресе закладки обрезал бы код — кодируем
  const href = 'javascript:' + encodeURI(code).replace(/#/g, '%23');

  const hint = `<svg viewBox="0 0 320 150" role="img" aria-label="Перетащите кнопку мышкой на панель закладок под адресной строкой" style="display:block;width:100%;max-width:380px;height:auto;font-family:inherit">
  <defs><marker id="dh-arrow" viewBox="0 0 10 10" refX="7" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0 10 5 0 10z" style="fill:var(--accent)"/></marker></defs>
  <rect x="1" y="1" width="318" height="148" rx="10" style="fill:var(--card);stroke:var(--line)"/>
  <path d="M1 27V11a10 10 0 0 1 10-10h298a10 10 0 0 1 10 10v16z" style="fill:var(--bg)"/>
  <circle cx="14" cy="14" r="3" style="fill:var(--line)"/><circle cx="24" cy="14" r="3" style="fill:var(--line)"/><circle cx="34" cy="14" r="3" style="fill:var(--line)"/>
  <rect x="48" y="7" width="214" height="14" rx="7" style="fill:var(--card);stroke:var(--line)"/>
  <text x="58" y="17" font-size="8" style="fill:var(--muted)">youtube.com/watch?v=…</text>
  <path d="M1 47h318" style="stroke:var(--line)"/>
  <rect x="10" y="32" width="38" height="10" rx="5" style="fill:var(--line)"/><rect x="54" y="32" width="30" height="10" rx="5" style="fill:var(--line)"/>
  <rect x="92" y="29.5" width="112" height="15" rx="7.5" stroke-dasharray="3 2" style="fill:var(--accent-soft);stroke:var(--accent)"/>
  <text x="148" y="40" font-size="8" text-anchor="middle" font-weight="600" style="fill:var(--accent)">⬇ Скачать в программе</text>
  <text x="212" y="40" font-size="8" style="fill:var(--muted)">← панель закладок</text>
  <rect x="14" y="58" width="128" height="76" rx="6" style="fill:var(--bg)"/>
  <path d="M71 84v24l20-12z" style="fill:var(--line)"/>
  <rect x="170" y="102" width="134" height="26" rx="8" style="fill:var(--accent)"/>
  <text x="237" y="119" font-size="9.5" text-anchor="middle" font-weight="600" fill="#fff">⬇ Скачать в программе</text>
  <path d="M214 98C218 70 182 70 168 52" fill="none" stroke-width="1.8" stroke-dasharray="4 3" marker-end="url(#dh-arrow)" style="stroke:var(--accent)"/>
  <path d="M250 112v15l3.6-3.4 2.6 5.6 2.4-1.1-2.6-5.5 5-.4z" stroke-width="1" stroke-linejoin="round" style="fill:#fff;stroke:#15171c"/>
  <text x="18" y="146" font-size="7.5" style="fill:var(--muted)">зажмите кнопку мышкой и тяните вверх</text>
</svg>`;

  const flash = (el, text) => {
    const box = el.closest('[data-bm-box]')?.querySelector('[data-bm-note]');
    if (!box) return;
    box.textContent = text;
    box.classList.add('on');
    clearTimeout(box._t);
    box._t = setTimeout(() => box.classList.remove('on'), 5000);
  };

  document.querySelectorAll('a[data-bookmarklet]').forEach(a => {
    a.href = href;
    a.title = 'Перетащите эту кнопку на панель закладок';
    a.addEventListener('click', e => {
      e.preventDefault();
      flash(a, 'Нажимать здесь не нужно — перетащите кнопку мышкой на панель закладок (картинка ниже).');
    });
  });
  document.querySelectorAll('[data-drag-hint]').forEach((el, i) => {
    el.innerHTML = i ? hint.replaceAll('dh-arrow', 'dh-arrow' + i) : hint;
  });
  document.querySelectorAll('[data-copy-bookmarklet]').forEach(b => b.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(href);
      flash(b, 'Код скопирован. Создайте закладку (Ctrl+D), откройте «Изменить» и вставьте код в поле адреса.');
    } catch {
      prompt('Скопируйте код закладки:', href);
    }
  }));
  window.udlBookmarklet = href;
})();
