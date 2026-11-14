"""Панель aiflow как устанавливаемое веб-приложение (PWA): манифест, значки, service worker.

Браузер разрешает установку со страницы на 127.0.0.1 без HTTPS. Установленное приложение получает своё окно,
свой значок на панели задач и счётчик на нём; службу оно не запускает — это делает автозапуск.
"""
from __future__ import annotations

import functools
import json
import math
import struct
import zlib

MANIFEST = {
    "name": "aiflow",
    "short_name": "aiflow",
    "description": "Очередь задач Claude Code: что ждёт вас и что идёт само",
    "id": "/",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "lang": "ru",
    "background_color": "#EDF0EC",
    "theme_color": "#EDF0EC",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
    ],
}

# Переходы по страницам — сначала сеть; служба не отвечает — страница «служба не запущена» из кэша.
SERVICE_WORKER = """const CACHE = "aiflow-v1";
self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.add("/offline.html")));
  self.skipWaiting();
});
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (e) => {
  if (e.request.mode !== "navigate") return;
  e.respondWith(fetch(e.request).catch(() => caches.match("/offline.html")));
});
"""

OFFLINE_PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>aiflow</title>
<style>
  body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #EDF0EC; color: #18211C;
         font: 15px/1.5 "Segoe UI", system-ui, sans-serif; }
  main { max-width: 460px; padding: 24px; }
  h1 { font-size: 22px; margin: 0 0 8px; }
  p { margin: 0 0 12px; color: #5B675F; }
  code { background: #F7F8F5; border: 1px solid #CDD5CE; border-radius: 4px; padding: 1px 6px; color: #18211C; }
</style>
</head>
<body>
<main>
  <h1>Служба aiflow не запущена</h1>
  <p>Очередь задач и панель ведёт служба. Запустите её командой <code>aiflow up</code> в любом терминале —
  эта страница откроет панель сама, как только служба ответит.</p>
  <p>Чтобы служба запускалась при входе в систему: <code>aiflow app</code>.</p>
</main>
<script>
  setInterval(async () => {
    try { const r = await fetch("/api/tray", { cache: "no-store" }); if (r.ok) location.replace("/"); } catch (e) {}
  }, 3000);
</script>
</body>
</html>
"""


def manifest_json() -> bytes:
    return json.dumps(MANIFEST, ensure_ascii=False).encode("utf-8")


def _png(size: int, rgba: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    rows = b"".join(b"\x00" + rgba[y * size * 4:(y + 1) * size * 4] for y in range(size))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


@functools.lru_cache(maxsize=4)
def icon_png(size: int) -> bytes:
    """Значок приложения: синий круг с белой буквой «a» — тот же, что рядом с часами. Рисуется без сторонних
    пакетов: расстояние до края каждой фигуры даёт сглаживание."""
    blue = (42, 120, 214)
    out = bytearray(size * size * 4)
    ring_c, ring_r, half_w = (0.455 * size, 0.56 * size), 0.155 * size, 0.0425 * size
    bar_c, bar_h, bar_rr = (0.655 * size, 0.545 * size), (0.0425 * size, 0.2125 * size), 0.04 * size
    centre, radius = size / 2, 0.47 * size

    def cover(d: float) -> float:  # d — расстояние до края фигуры, отрицательное внутри
        return 0.0 if d >= 0.5 else 1.0 if d <= -0.5 else 0.5 - d

    i = 0
    for y in range(size):
        py = y + 0.5
        for x in range(size):
            px = x + 0.5
            a = cover(math.hypot(px - centre, py - centre) - radius)
            if a > 0.0:
                ring = abs(math.hypot(px - ring_c[0], py - ring_c[1]) - ring_r) - half_w
                qx = abs(px - bar_c[0]) - bar_h[0] + bar_rr
                qy = abs(py - bar_c[1]) - bar_h[1] + bar_rr
                bar = math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - bar_rr
                w = cover(min(ring, bar))
                out[i] = int(blue[0] + (255 - blue[0]) * w)
                out[i + 1] = int(blue[1] + (255 - blue[1]) * w)
                out[i + 2] = int(blue[2] + (255 - blue[2]) * w)
                out[i + 3] = int(255 * a)
            i += 4
    return _png(size, bytes(out))
