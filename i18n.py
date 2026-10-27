"""Язык программы: русский или английский. «lang» в settings.json; пусто — как язык Windows.
Только stdlib: нужен до тяжёлых импортов (заставка) и в sites.py."""
import os

LANGS = ('ru', 'en')
# языки Windows, у которых по умолчанию русский интерфейс (остальным — английский)
RU_SYSTEM = {0x19, 0x22, 0x23, 0x3F, 0x40, 0x43, 0x28}  # ru, uk, be, kk, ky, uz, tg
CURRENT = 'ru'


def system_lang():
    if os.name != 'nt':
        return 'ru' if os.environ.get('LANG', '').startswith('ru') else 'en'
    try:
        import ctypes
        return 'ru' if ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF in RU_SYSTEM else 'en'
    except (OSError, AttributeError):
        return 'ru'


def set_lang(value):
    """value — 'ru', 'en' или пусто (как в Windows). Возвращает выбранный язык."""
    global CURRENT
    CURRENT = value if value in LANGS else system_lang()
    return CURRENT


def tr(ru, en):
    """Текст на текущем языке: tr('Готово', 'Done')."""
    return en if CURRENT == 'en' else ru
