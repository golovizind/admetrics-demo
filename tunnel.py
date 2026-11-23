#!/usr/bin/env python3
"""
Надзиратель за HTTPS-туннелем к панели.

Туннель нельзя «запустить и забыть», и это выяснилось на практике:

  • localhost.run рвёт соединение по неактивности — панель не открывали
    сутки, и адрес умер молча;
  • при каждом подъёме выдаётся НОВЫЙ адрес;
  • несколько запусков подряд оставляли висеть чужие ssh-сессии, и в файле
    оказывался адрес уже мёртвой из них — кнопка вела в «no tunnel here».

Поэтому туннель нужно не запускать, а держать: этот процесс следит за
ssh, поднимает заново после обрыва, раз в несколько минут дёргает панель
через публичный адрес (чтобы соединение не признали простаивающим) и
пишет актуальный адрес в `.webapp_url`.

Бота перезапускать не нужно: он читает файл при каждой отрисовке меню,
так что кнопка «Открыть панель» подхватывает новый адрес сама. Пока
туннеля нет, файл пуст — и кнопка просто не рисуется, вместо того чтобы
вести на мёртвую страницу.

Запуск:
    python3 tunnel.py --port 8080
"""

from __future__ import annotations

import argparse
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

URL_FILE = Path(__file__).parent / ".webapp_url"
URL_RE = re.compile(r"https://[a-z0-9.-]+\.lhr\.life")
# Сессии бесплатных туннелей живут минутами, поэтому проверяем часто и
# поднимаем заново с первой же неудачи: лишний перезапуск дешевле, чем
# минуты, в которые панель открывается в «no tunnel here».
PING_EVERY = 45
# Одной неудачной проверки мало: у бесплатного туннеля таймаут ловится
# регулярно, а каждый перезапуск выдаёт НОВЫЙ адрес. Рвём только после
# трёх подряд — тогда адрес не прыгает от случайной икоты.
PING_FAILS_BEFORE_RESTART = 3


def log(message: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)


def kill_strays() -> None:
    """Прибить чужие ssh-сессии к localhost.run.

    Их появление — не гипотеза: три висящих сессии однажды и дали адрес
    от мёртвой, потому что в файл попадал не тот туннель, который жив.
    """
    for pattern in ("ssh.*localhost[.]run", "cloudflared tunnel --url"):
        subprocess.run(["pkill", "-f", pattern], capture_output=True)


def spawn_lhr(port: int) -> tuple[subprocess.Popen, str]:
    """Туннель через localhost.run — запасной путь, когда DNS сломан."""
    proc = subprocess.Popen(
        ["ssh", "-o", "StrictHostKeyChecking=accept-new",
         "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3",
         "-o", "ExitOnForwardFailure=yes",
         "-R", f"80:localhost:{port}", "nokey@localhost.run"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

    url = ""
    deadline = time.time() + 45
    while time.time() < deadline:
        line = proc.stdout.readline()  # type: ignore[union-attr]
        if not line:
            break
        found = URL_RE.search(line)
        if found:
            url = found.group(0)
            break
    # Дальше вывод только копится в трубе: не читать его — значит рано или
    # поздно заблокировать ssh на полном буфере.
    threading.Thread(target=lambda: [None for _ in proc.stdout],  # type: ignore[union-attr]
                     daemon=True).start()
    return proc, url



CF_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def cloudflared_usable() -> bool:
    """Может ли cloudflared работать на этой машине.

    Он написан на Go, а Go читает `/etc/resolv.conf` напрямую, минуя
    системный резолвер macOS. Если файла нет или в нём только localhost,
    все DNS-запросы падают, и туннель не поднимается вовсе — проверяем
    это заранее, чтобы не уходить в бесконечные перезапуски.
    """
    if not shutil.which("cloudflared"):
        return False
    try:
        conf = Path("/etc/resolv.conf").read_text(encoding="utf-8")
    except OSError:
        return False
    servers = [line.split()[1] for line in conf.splitlines()
               if line.startswith("nameserver") and len(line.split()) > 1]
    return any(s not in ("::1", "127.0.0.1") for s in servers)


def spawn_cloudflared(port: int) -> tuple[subprocess.Popen, str]:
    proc = subprocess.Popen(
        ["cloudflared", "tunnel", "--url", f"http://127.0.0.1:{port}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    url = ""
    deadline = time.time() + 45
    while time.time() < deadline:
        line = proc.stdout.readline()  # type: ignore[union-attr]
        if not line:
            break
        found = CF_RE.search(line)
        if found:
            url = found.group(0)
            break
    threading.Thread(target=lambda: [None for _ in proc.stdout],  # type: ignore[union-attr]
                     daemon=True).start()
    return proc, url


def alive(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=25) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001 — любая ошибка здесь значит «не отвечает»
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Держать HTTPS-туннель к панели")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    backoff = 5
    while not stop.is_set():
        kill_strays()
        URL_FILE.write_text("", encoding="utf-8")
        use_cf = cloudflared_usable()
        proc, url = (spawn_cloudflared if use_cf else spawn_lhr)(args.port)
        if not url:
            proc.terminate()
            log(f"адрес не получен, повтор через {backoff} с")
            stop.wait(backoff)
            backoff = min(backoff * 2, 120)
            continue

        URL_FILE.write_text(url, encoding="utf-8")
        log(f"туннель поднят ({'cloudflared' if use_cf else 'localhost.run'}): {url}")
        backoff = 5
        fails = 0

        while not stop.is_set():
            if stop.wait(PING_EVERY):
                break
            if proc.poll() is not None:
                log("ssh завершился — поднимаю заново")
                break
            # Самопинг и есть та «активность», из-за отсутствия которой
            # localhost.run закрывал соединение.
            if alive(url):
                fails = 0
                continue
            fails += 1
            log(f"панель не отвечает через туннель ({fails})")
            if fails >= PING_FAILS_BEFORE_RESTART:
                log("поднимаю туннель заново")
                proc.terminate()
                break

        if proc.poll() is None:
            proc.terminate()

    kill_strays()
    URL_FILE.write_text("", encoding="utf-8")
    log("остановлен")
    return 0


if __name__ == "__main__":
    sys.exit(main())
