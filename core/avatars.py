"""Аватарки профилей из Telegram: тянем через Bot API и кэшируем на диск.

Профиль-фото у Telegram отдаётся в два шага (getUserProfilePhotos → getFile),
файл живёт по временной ссылке. Дёргать это на каждый показ портала нельзя —
кэшируем картинку рядом с данными и обновляем не чаще раза в сутки. Отдельно
помним «фото нет / приватность», чтобы не долбить API по тем, у кого его нет.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

_TTL = 24 * 3600          # как долго годна закэшированная картинка
_NEG_TTL = 6 * 3600       # как долго помним «фото нет»
_DIR = Path(os.environ.get(
    "ADMETRICS_AVATAR_DIR",
    str(Path(__file__).resolve().parent.parent / "data" / "avatars")))


def _api(token: str, method: str, **params: object) -> dict:
    url = (f"https://api.telegram.org/bot{token}/{method}?"
           + urllib.parse.urlencode(params))
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)


def _download(token: str, file_id: str, dest: Path) -> bool:
    info = _api(token, "getFile", file_id=file_id)
    if not info.get("ok"):
        return False
    path = info["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{token}/{path}"
    with urllib.request.urlopen(url, timeout=15) as r:
        data = r.read()
    dest.write_bytes(data)
    return True


def avatar_bytes(tg_id: object, token: str) -> bytes | None:
    """JPEG-байты аватарки Telegram-профиля или None. Кэш на диске, TTL сутки."""
    if not tg_id or not token:
        return None
    tg = str(tg_id)
    if not tg.isdigit():
        return None
    _DIR.mkdir(parents=True, exist_ok=True)
    img = _DIR / f"{tg}.jpg"
    miss = _DIR / f"{tg}.none"
    now = time.time()
    if img.exists() and now - img.stat().st_mtime < _TTL:
        return img.read_bytes()
    if miss.exists() and now - miss.stat().st_mtime < _NEG_TTL:
        return None
    try:
        photos = _api(token, "getUserProfilePhotos", user_id=tg, limit=1)
        sizes = ((photos.get("result") or {}).get("photos") or [[]])[0]
        if not sizes:
            miss.write_text("", encoding="utf-8")
            return None
        # средний размер: для 46px крупный оригинал не нужен
        size = sizes[min(1, len(sizes) - 1)]
        if _download(token, size["file_id"], img):
            miss.unlink(missing_ok=True)
            return img.read_bytes()
    except Exception:
        # сеть или приватность закрыта — отдаём старый кэш, если есть
        if img.exists():
            return img.read_bytes()
    return None
