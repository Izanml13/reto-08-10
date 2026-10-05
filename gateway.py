"""Gateway optimizado delante del backend LLM (compatible con OpenAI).

Optimizaciones:
  1. Caché de respuestas frecuentes: LRU con TTL, con clave = modelo + mensajes.
  2. Coalescencia de peticiones (single-flight): si llegan N peticiones idénticas a la vez,
     solo una llega al modelo y el resto espera su resultado.
  3. Ajuste de parámetros: limita max_tokens y baja la temperatura, para respuestas más
     cortas, más deterministas y más cacheables.

    python gateway.py                      # escucha en :8080 y reenvía a Ollama local
    UPSTREAM_URL=http://vm:8000 python gateway.py
"""

import asyncio
import hashlib
import json
import os
import time
from collections import OrderedDict

from aiohttp import ClientSession, ClientTimeout, web

UPSTREAM_URL = os.getenv("UPSTREAM_URL", "http://localhost:11434").rstrip("/")
UPSTREAM_API_KEY = os.getenv("UPSTREAM_API_KEY", "")
MAX_TOKENS = int(os.getenv("GW_MAX_TOKENS", "128"))
TEMPERATURE = float(os.getenv("GW_TEMPERATURE", "0.2"))
CACHE_SIZE = int(os.getenv("GW_CACHE_SIZE", "500"))
CACHE_TTL = int(os.getenv("GW_CACHE_TTL", "3600"))
PORT = int(os.getenv("GW_PORT", "8080"))


class CacheLRU:
    def __init__(self, tamano, ttl):
        self.tamano, self.ttl = tamano, ttl
        self.datos = OrderedDict()
        self.aciertos = self.fallos = 0

    def get(self, clave):
        item = self.datos.get(clave)
        if item and time.time() - item[0] < self.ttl:
            self.datos.move_to_end(clave)
            self.aciertos += 1
            return item[1]
        self.datos.pop(clave, None)
        self.fallos += 1
        return None

    def set(self, clave, valor):
        self.datos[clave] = (time.time(), valor)
        self.datos.move_to_end(clave)
        while len(self.datos) > self.tamano:
            self.datos.popitem(last=False)


cache = CacheLRU(CACHE_SIZE, CACHE_TTL)
en_vuelo: dict[str, asyncio.Future] = {}


def clave_cache(cuerpo):
    base = {"model": cuerpo.get("model"), "messages": cuerpo.get("messages")}
    return hashlib.sha256(json.dumps(base, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


async def reenviar(app, cuerpo):
    cabeceras = {"Authorization": f"Bearer {UPSTREAM_API_KEY}"} if UPSTREAM_API_KEY else {}
    async with app["sesion"].post(f"{UPSTREAM_URL}/v1/chat/completions", json=cuerpo, headers=cabeceras) as r:
        return r.status, await r.json(content_type=None)


async def chat(request):
    cuerpo = await request.json()
    # Optimización 3: ajuste de parámetros
    cuerpo["max_tokens"] = min(cuerpo.get("max_tokens") or MAX_TOKENS, MAX_TOKENS)
    cuerpo["temperature"] = TEMPERATURE
    clave = clave_cache(cuerpo)

    # Optimización 1: caché
    if (respuesta := cache.get(clave)) is not None:
        return web.json_response(respuesta, headers={"X-Cache": "HIT"})

    # Optimización 2: coalescencia de peticiones idénticas en vuelo
    if clave in en_vuelo:
        estado, datos = await asyncio.shield(en_vuelo[clave])
        return web.json_response(datos, status=estado, headers={"X-Cache": "HIT"})

    futuro = asyncio.get_running_loop().create_future()
    en_vuelo[clave] = futuro
    try:
        estado, datos = await reenviar(request.app, cuerpo)
        if estado == 200:
            cache.set(clave, datos)
        futuro.set_result((estado, datos))
    except Exception as e:
        estado, datos = 502, {"error": str(e)}
        futuro.set_result((estado, datos))
    finally:
        en_vuelo.pop(clave, None)
    return web.json_response(datos, status=estado, headers={"X-Cache": "MISS"})


async def modelos(request):
    async with request.app["sesion"].get(f"{UPSTREAM_URL}/v1/models") as r:
        return web.json_response(await r.json(content_type=None), status=r.status)


async def stats(request):
    total = cache.aciertos + cache.fallos
    return web.json_response({"entradas": len(cache.datos), "aciertos": cache.aciertos, "fallos": cache.fallos,
                              "tasa_acierto_pct": round(100 * cache.aciertos / total, 1) if total else 0,
                              "max_tokens": MAX_TOKENS, "temperature": TEMPERATURE, "upstream": UPSTREAM_URL})


async def vaciar(request):
    cache.datos.clear()
    cache.aciertos = cache.fallos = 0
    return web.json_response({"ok": True})


async def al_iniciar(app):
    app["sesion"] = ClientSession(timeout=ClientTimeout(total=600))


async def al_cerrar(app):
    await app["sesion"].close()


def crear_app():
    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    app.router.add_get("/v1/models", modelos)
    app.router.add_get("/stats", stats)
    app.router.add_post("/cache/clear", vaciar)
    app.on_startup.append(al_iniciar)
    app.on_cleanup.append(al_cerrar)
    return app


if __name__ == "__main__":
    print(f"Gateway en :{PORT} → {UPSTREAM_URL} · max_tokens={MAX_TOKENS} · temperature={TEMPERATURE}")
    web.run_app(crear_app(), port=PORT, print=None)
