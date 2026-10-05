"""Pruebas de estrés con asyncio + aiohttp contra backends con API compatible con OpenAI.

Cada escenario simula N usuarios simultáneos; cada uno envía varias peticiones seguidas.
Se registran latencias, errores, tokens/s y el uso de CPU/RAM durante la prueba.

    python stress_test.py --backend local --etiqueta antes
    python stress_test.py --backend gateway --etiqueta despues
    python stress_test.py --backend azure --etiqueta antes     # requiere .env
"""

import argparse
import asyncio
import json
import os
import random
import statistics
import time
from datetime import datetime
from pathlib import Path

import aiohttp
import psutil

# Preguntas «frecuentes», con pesos tipo Zipf: unas se repiten mucho más que otras,
# como en un chatbot real. Esto permite medir el efecto de la caché.
PROMPTS = [
    (8, "¿Qué es Docker? Responde en 2 frases."),
    (6, "¿Qué diferencia hay entre una imagen y un contenedor?"),
    (5, "Explica qué es la cuantización Q4 de un modelo."),
    (4, "Dame 3 consejos para escribir buenos prompts."),
    (3, "¿Qué es una API REST?"),
    (2, "Escribe una función Python que invierta una cadena."),
    (1, "¿Qué ventajas tiene ejecutar un LLM en local?"),
    (1, "Resume qué es Kubernetes en una frase."),
]


def cargar_env(ruta=".env"):
    if Path(ruta).exists():
        for linea in Path(ruta).read_text().splitlines():
            linea = linea.strip()
            if linea and not linea.startswith("#") and "=" in linea:
                k, v = linea.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


def config_backend(nombre):
    if nombre == "local":
        return {"url": os.getenv("OLLAMA_URL", "http://localhost:11434"),
                "modelo": os.getenv("OLLAMA_MODEL", "gemma4:e4b"), "api_key": ""}
    if nombre == "gateway":
        return {"url": os.getenv("GATEWAY_URL", "http://localhost:8080"),
                "modelo": os.getenv("OLLAMA_MODEL", "gemma4:e4b"), "api_key": ""}
    if nombre == "azure":
        return {"url": os.getenv("AZURE_VM_URL", ""), "modelo": os.getenv("AZURE_VM_MODEL", ""),
                "api_key": os.getenv("AZURE_VM_API_KEY", "")}
    raise ValueError(nombre)


class MonitorRecursos:
    """Muestrea CPU y RAM del sistema y de los procesos de Ollama cada 0,5 s."""

    def __init__(self, nombres=("ollama", "llama-server")):
        self.nombres = nombres
        self.muestras = []
        self._tarea = None

    def _rss_procesos(self):
        total = 0
        for p in psutil.process_iter(["name", "memory_info"]):
            try:
                if any(n in (p.info["name"] or "").lower() for n in self.nombres):
                    total += p.info["memory_info"].rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return total

    async def _bucle(self):
        psutil.cpu_percent(None)
        while True:
            await asyncio.sleep(0.5)
            self.muestras.append({
                "cpu_pct": psutil.cpu_percent(None),
                "ram_sistema_gb": psutil.virtual_memory().used / 1e9,
                "ram_ollama_gb": self._rss_procesos() / 1e9,
            })

    def iniciar(self):
        self._tarea = asyncio.create_task(self._bucle())

    async def detener(self):
        self._tarea.cancel()
        try:
            await self._tarea
        except asyncio.CancelledError:
            pass
        if not self.muestras:
            return {}
        cpu = [m["cpu_pct"] for m in self.muestras]
        return {
            "cpu_media_pct": round(statistics.mean(cpu), 1),
            "cpu_max_pct": round(max(cpu), 1),
            "ram_sistema_max_gb": round(max(m["ram_sistema_gb"] for m in self.muestras), 2),
            "ram_ollama_max_gb": round(max(m["ram_ollama_gb"] for m in self.muestras), 2),
        }


async def peticion(sesion, cfg, prompt, args):
    cuerpo = {
        "model": cfg["modelo"],
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": args.max_tokens,
        "temperature": args.temperature,
        "reasoning_effort": "none",  # Ollama: sin modo thinking
    }
    cabeceras = {"Authorization": f"Bearer {cfg['api_key']}"} if cfg["api_key"] else {}
    inicio = time.perf_counter()
    try:
        async with sesion.post(f"{cfg['url'].rstrip('/')}/v1/chat/completions",
                               json=cuerpo, headers=cabeceras) as r:
            datos = await r.json(content_type=None)
            latencia = time.perf_counter() - inicio
            if r.status != 200:
                return {"ok": False, "latencia_s": latencia, "error": f"HTTP {r.status}"}
            tokens = datos.get("usage", {}).get("completion_tokens", 0)
            return {"ok": True, "latencia_s": latencia, "tokens": tokens,
                    "cache": r.headers.get("X-Cache", "-")}
    except asyncio.TimeoutError:
        return {"ok": False, "latencia_s": time.perf_counter() - inicio, "error": "timeout"}
    except aiohttp.ClientError as e:
        return {"ok": False, "latencia_s": time.perf_counter() - inicio, "error": type(e).__name__}


async def usuario(sesion, cfg, args, rng, resultados):
    pesos, textos = zip(*PROMPTS)
    for _ in range(args.peticiones):
        prompt = rng.choices(textos, weights=pesos)[0]
        resultados.append(await peticion(sesion, cfg, prompt, args))


def percentil(valores, p):
    if not valores:
        return None
    valores = sorted(valores)
    k = (len(valores) - 1) * p / 100
    f, c = int(k), min(int(k) + 1, len(valores) - 1)
    return valores[f] + (valores[c] - valores[f]) * (k - f)


async def escenario(cfg, n_usuarios, args):
    resultados = []
    monitor = MonitorRecursos()
    timeout = aiohttp.ClientTimeout(total=args.timeout)
    conector = aiohttp.TCPConnector(limit=0)  # sin límite: queremos toda la concurrencia
    async with aiohttp.ClientSession(timeout=timeout, connector=conector) as sesion:
        monitor.iniciar()
        inicio = time.perf_counter()
        await asyncio.gather(*(usuario(sesion, cfg, args, random.Random(args.semilla + i), resultados)
                               for i in range(n_usuarios)))
        duracion = time.perf_counter() - inicio
        recursos = await monitor.detener()

    ok = [r for r in resultados if r["ok"]]
    lat = [r["latencia_s"] for r in ok]
    tokens = sum(r["tokens"] for r in ok)
    tok_s_req = [r["tokens"] / r["latencia_s"] for r in ok if r["latencia_s"] > 0 and r["tokens"]]
    errores = {}
    for r in resultados:
        if not r["ok"]:
            errores[r["error"]] = errores.get(r["error"], 0) + 1
    return {
        "usuarios": n_usuarios,
        "peticiones": len(resultados),
        "ok": len(ok),
        "errores": len(resultados) - len(ok),
        "tasa_error_pct": round(100 * (len(resultados) - len(ok)) / len(resultados), 1),
        "detalle_errores": errores,
        "duracion_s": round(duracion, 1),
        "lat_media_s": round(statistics.mean(lat), 2) if lat else None,
        "lat_p50_s": round(percentil(lat, 50), 2) if lat else None,
        "lat_p95_s": round(percentil(lat, 95), 2) if lat else None,
        "lat_max_s": round(max(lat), 2) if lat else None,
        "throughput_req_s": round(len(ok) / duracion, 3),
        "tok_s_agregado": round(tokens / duracion, 1),
        "tok_s_por_peticion": round(statistics.mean(tok_s_req), 1) if tok_s_req else None,
        "aciertos_cache": sum(1 for r in ok if r.get("cache") == "HIT"),
        **recursos,
    }


async def main_async(args):
    cfg = config_backend(args.backend)
    if not cfg["url"] or "<" in cfg["url"]:
        raise SystemExit(f"Backend '{args.backend}' sin configurar (revisa .env)")
    print(f"Backend: {args.backend} → {cfg['url']} · modelo {cfg['modelo']} · "
          f"max_tokens={args.max_tokens} · temperature={args.temperature}")

    # Calentamiento: carga el modelo en memoria para no medir el arranque en frío
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as s:
        await peticion(s, cfg, "Hola", args)

    escenarios = []
    for n in args.usuarios:
        if args.vaciar_cache:
            # Cada escenario empieza con la caché vacía para que sea comparable con el resto
            async with aiohttp.ClientSession() as s:
                await s.post(f"{cfg['url'].rstrip('/')}/cache/clear")
        print(f"\n▶ {n} usuarios × {args.peticiones} peticiones ...", flush=True)
        res = await escenario(cfg, n, args)
        escenarios.append(res)
        print(f"  p50 {res['lat_p50_s']} s · p95 {res['lat_p95_s']} s · errores {res['errores']} · "
              f"{res['tok_s_agregado']} tok/s agregados · CPU media {res.get('cpu_media_pct')} % · "
              f"caché {res['aciertos_cache']}")

    Path("resultados").mkdir(exist_ok=True)
    salida = Path("resultados", f"{args.backend}_{args.etiqueta}.json")
    salida.write_text(json.dumps({
        "backend": args.backend, "etiqueta": args.etiqueta, "url": cfg["url"], "modelo": cfg["modelo"],
        "fecha": datetime.now().isoformat(timespec="seconds"),
        "parametros": {"max_tokens": args.max_tokens, "temperature": args.temperature,
                       "peticiones_por_usuario": args.peticiones, "timeout_s": args.timeout},
        "escenarios": escenarios,
    }, ensure_ascii=False, indent=2))
    print(f"\nGuardado en {salida}")


def main():
    cargar_env()
    p = argparse.ArgumentParser(description="Pruebas de estrés de backends LLM")
    p.add_argument("--backend", choices=["local", "gateway", "azure"], required=True)
    p.add_argument("--etiqueta", default="antes", help="antes / despues")
    p.add_argument("--usuarios", type=int, nargs="+", default=[5, 10, 20])
    p.add_argument("--peticiones", type=int, default=2, help="Peticiones por usuario")
    p.add_argument("--max-tokens", type=int, default=256)
    p.add_argument("--temperature", type=float, default=0.7)
    p.add_argument("--timeout", type=int, default=180, help="Timeout por petición (s)")
    p.add_argument("--vaciar-cache", action="store_true", help="Gateway: vaciar caché antes de cada escenario")
    p.add_argument("--semilla", type=int, default=42)
    asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    main()
