// Программа не запущена: включаем её ссылкой udl-downloader: (её прописывает в Windows сама программа при
// каждом запуске) и ждём, пока страница программы заработает, — тогда переходим на неё со ссылкой из #.
const BASE = `http://127.0.0.1:${self.UDL_PORT}/`;
const link = decodeURIComponent(location.hash.slice(1));

function start() {
  location.href = 'udl-downloader:start';
}

async function up() {
  try {
    await fetch(BASE + 'favicon.ico', {method: 'HEAD', cache: 'no-store'});
    return true;
  } catch {
    return false;
  }
}

async function wait() {
  for (let i = 0; i < 600; i++) {
    if (await up()) {
      location.replace(link ? `${BASE}?url=${encodeURIComponent(link)}` : BASE);
      return;
    }
    if (i === 15) document.body.classList.add('is-slow');
    await new Promise(r => setTimeout(r, 1000));
  }
}

document.getElementById('start').addEventListener('click', start);
start();
wait();
