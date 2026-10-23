"""Обновление самой программы с GitHub.

app.py раз в три дня спрашивает GitHub о последнем релизе (check). Пользователь нажимает «Обновить» →
download() качает zip, сверяет SHA-256 из GitHub и раскладывает новую версию в update\\new.
Заменить файлы, пока программа работает, нельзя, поэтому app.py запускает этот файл отдельным процессом
(копией из update\\) и закрывается. Он ждёт, пока программа выйдет, переносит старые файлы в update\\old,
новые — на их место, и запускает launch.vbs (поменялся requirements.txt — тот сам доставит пакеты через
start.ps1). Не вышло (файл занят, новая версия не поднялась) — возвращает старые файлы и запускает их.
Итог — в update\\result.json: его показывает страница после перезапуска.

Только стандартная библиотека: этот файл работает и без пакетов программы.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ASSET = 'universal-downloader.zip'  # имя файла в релизе; на него же ссылается README
MAX_SIZE = 50 << 20
# Личное и то, что ставится само, — даже если оказалось в архиве, не трогаем
KEEP = {'settings.json', 'cookies.txt', 'program.log', 'Скачанное', 'python', 'python.new', 'bin',
        '.venv', '.git', 'update', 'dist', 'tests', 'CLAUDE.md'}
NEED = ('app.py', 'launch.vbs', 'start.ps1', 'requirements.txt', 'updater.py', 'web/index.html')
VERSION_RE = re.compile(r"^VERSION = '([\d.]+)'", re.M)


def parse_version(text):
    """'v1.2' или '1.2' → (1, 2); не номер версии → None."""
    m = re.fullmatch(r'v?(\d+(?:\.\d+)*)', (text or '').strip())
    return tuple(int(x) for x in m.group(1).split('.')) if m else None


def check(current, open_url, api):
    """Последний релиз на GitHub, если он новее current, иначе None.
    open_url(url) → ответ с read(); сеть — забота app.py (там прокси)."""
    with open_url(api) as r:
        rel = json.loads(r.read())
    latest = parse_version(rel.get('tag_name'))
    if not latest or latest <= parse_version(current):
        return None
    asset = next((a for a in rel.get('assets') or () if a.get('name') == ASSET), None)
    if not asset:
        return None  # релиз ещё собирают: архив не приложен
    digest = asset.get('digest') or ''
    return {
        'version': '.'.join(map(str, latest)),
        'notes': rel.get('body') or '',
        'page': rel.get('html_url') or '',
        'url': asset['browser_download_url'],
        'size': asset.get('size') or 0,
        'sha256': digest[7:].lower() if digest.startswith('sha256:') else '',
    }


def download(info, open_url, work, progress=lambda done, total: None):
    """Качает архив версии info в папку work, проверяет его и распаковывает. Возвращает папку новой версии.
    Ошибки — ValueError с понятным текстом."""
    if not re.fullmatch(r'[0-9a-f]{64}', info.get('sha256') or ''):
        raise ValueError('у архива на GitHub нет контрольной суммы — скачайте новую версию вручную')
    if info.get('size', 0) > MAX_SIZE:
        raise ValueError('архив обновления подозрительно большой')
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    zpath = work / ASSET
    h = hashlib.sha256()
    done = 0
    with open_url(info['url']) as r, open(zpath, 'wb') as f:
        while chunk := r.read(64 << 10):
            done += len(chunk)
            if done > MAX_SIZE:
                raise ValueError('архив обновления подозрительно большой')
            h.update(chunk)
            f.write(chunk)
            progress(done, info.get('size') or done)
    if h.hexdigest() != info['sha256']:
        raise ValueError('архив скачался с ошибкой (не совпала контрольная сумма) — попробуйте ещё раз')
    new = work / 'new'
    try:
        with zipfile.ZipFile(zpath) as z:
            z.extractall(new)  # zipfile сам отбрасывает «..» и абсолютные пути
    except zipfile.BadZipFile:
        raise ValueError('архив обновления повреждён') from None
    roots = [p.parent for p in new.glob('*/app.py')] + [p.parent for p in new.glob('app.py')]
    if len(roots) != 1:
        raise ValueError('в архиве обновления не нашлась программа')
    root = roots[0]
    missing = [n for n in NEED if not (root / n).is_file()]
    if missing:
        raise ValueError('в архиве обновления нет файлов: ' + ', '.join(missing))
    m = VERSION_RE.search((root / 'app.py').read_text(encoding='utf-8'))
    if not m or parse_version(m.group(1)) != parse_version(info['version']):
        raise ValueError('номер версии в архиве не совпадает с релизом')
    for py in root.glob('*.py'):  # битый код не ставим: старая версия хотя бы работает
        try:
            compile(py.read_text(encoding='utf-8'), str(py), 'exec')
        except (SyntaxError, ValueError, UnicodeDecodeError) as e:
            raise ValueError(f'в архиве обновления испорчен {py.name}: {e}') from None
    return root


# ---- всё, что ниже, работает отдельным процессом, когда программа уже закрылась ----

def log(work, text):
    with open(work / 'update.log', 'a', encoding='utf-8') as f:
        f.write(time.strftime('%H:%M:%S ') + text + '\n')


def wait_exit(pid, timeout):
    """Ждёт, пока процесс программы завершится. False — так и не завершился."""
    import ctypes
    kernel = ctypes.windll.kernel32
    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return True  # процесса уже нет
    try:
        return kernel.WaitForSingleObject(handle, int(timeout * 1000)) == 0
    finally:
        kernel.CloseHandle(handle)


def retry(action, *args):
    """Антивирус или Проводник иногда на миг держат файл — пробуем ещё несколько раз."""
    for i in range(20):
        try:
            return action(*args)
        except PermissionError:
            if i == 19:
                raise
            time.sleep(0.5)


def swap(base, new, old):
    """Новые файлы — на место старых, старые — в old. Список сделанного — для отката."""
    done = []
    old.mkdir(exist_ok=True)
    for item in sorted(new.iterdir()):
        if item.name in KEEP or item.name == '__pycache__':
            continue
        target, backup = base / item.name, old / item.name
        had = target.exists()
        if had:
            retry(os.replace, target, backup)
        done.append((item.name, had))
        retry(os.replace, item, target)
    return done


def rollback(base, new, old, done):
    for name, had in reversed(done):
        target = base / name
        if target.exists():
            retry(os.replace, target, new / name)
        if had:
            retry(os.replace, old / name, target)


def server_version(port):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f'http://127.0.0.1:{port}/api/status', timeout=5) as r:
            return json.loads(r.read()).get('version') or '?'
    except Exception:  # noqa: BLE001
        return None


def clean(work):
    """Скачанное больше не нужно; журнал и result.json остаются."""
    shutil.rmtree(work / 'old', ignore_errors=True)
    shutil.rmtree(work / 'new', ignore_errors=True)
    (work / ASSET).unlink(missing_ok=True)


def launch(base):
    wscript = Path(os.environ.get('SystemRoot', 'C:\\Windows')) / 'System32' / 'wscript.exe'
    subprocess.Popen([str(wscript), str(base / 'launch.vbs')], cwd=base)


def apply(args):
    base, work = Path(args.base), Path(args.work)
    new, old = Path(args.new), work / 'old'
    result = work / 'result.json'

    def finish(ok, error=''):
        result.write_text(json.dumps({'ok': ok, 'from': args.old_version, 'to': args.version, 'error': error},
                                     ensure_ascii=False), encoding='utf-8')
        log(work, f'итог: {"готово" if ok else error}')

    log(work, f'обновление {args.old_version} → {args.version}')
    if not wait_exit(args.pid, 60):
        finish(False, 'программа не закрылась — обновление отложено')
        return
    reqs_before = (base / 'requirements.txt').read_bytes() if (base / 'requirements.txt').exists() else b''
    shutil.rmtree(old, ignore_errors=True)
    done = []
    try:
        done = swap(base, new, old)
    except OSError as e:
        log(work, f'не удалось заменить файлы: {e}')
        try:
            rollback(base, new, old, done)
        except OSError as e2:
            log(work, f'откат не удался: {e2}')
        finish(False, f'файл занят другой программой ({e.filename or e}) — работает прежняя версия')
        launch(base)
        return
    finish(True)
    launch(base)
    # requirements.txt поменялся — launch.vbs открыл окно установки пакетов, это может занять минуты:
    # тогда только ждём, а откатывать не будем (ошибки установки видны в том окне)
    reqs_changed = (base / 'requirements.txt').read_bytes() != reqs_before
    deadline = time.time() + (900 if reqs_changed else 180)
    while time.time() < deadline:
        if server_version(args.port) == args.version:
            log(work, 'новая версия запустилась')
            clean(work)
            return
        time.sleep(1)
    if reqs_changed or server_version(args.port) is not None:
        log(work, 'новая версия не ответила вовремя, но порт занят или ставятся пакеты — не откатываем')
        return
    log(work, 'новая версия не запустилась — возвращаю прежнюю')
    try:
        rollback(base, new, old, done)
    except OSError as e:
        log(work, f'откат не удался: {e}')
        return
    finish(False, f'версия {args.version} не запустилась — вернули прежнюю')
    clean(work)
    launch(base)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    for name in ('base', 'work', 'new', 'version', 'old-version'):
        p.add_argument('--' + name, required=True)
    p.add_argument('--pid', type=int, required=True)
    p.add_argument('--port', type=int, required=True)
    try:
        apply(p.parse_args())
    except Exception as e:  # noqa: BLE001
        def arg(name):
            return Path(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else Path('.')
        log(arg('--work'), f'сбой: {e!r}')
        launch(arg('--base'))  # программа уже закрылась — без неё совсем ничего не будет
