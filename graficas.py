"""Genera las gráficas de rendimiento a partir de resultados/*.json → informe/img/."""

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

VERDE, SALVIA, AMBAR, GRIS = "#1B4332", "#52796F", "#D97706", "#c1c8c2"
AMBAR_CLARO = "#E9C46A"
plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False,
                     "axes.edgecolor": "#717973", "axes.titleweight": "bold", "axes.titlesize": 11})

SERIES = [  # (archivo, etiqueta, color)
    ("local_antes", "Local · antes", SALVIA),
    ("local_parametros", "Local · solo ajuste de parámetros", AMBAR_CLARO),
    ("gateway_despues", "Local · después (caché + parámetros)", VERDE),
    ("azure_antes", "Azure · antes", "#8d99ae"),
    ("azure_despues", "Azure · después", "#2b2d42"),
]


def cargar():
    datos = {}
    for archivo, etiqueta, color in SERIES:
        ruta = Path("resultados", f"{archivo}.json")
        if ruta.exists():
            datos[archivo] = (etiqueta, color, json.loads(ruta.read_text())["escenarios"])
    return datos


def barras(ax, datos, metrica, titulo, unidad):
    n = len(datos)
    ancho = 0.8 / max(n, 1)
    escenarios = None
    for i, (etiqueta, color, esc) in enumerate(datos.values()):
        escenarios = [e["usuarios"] for e in esc]
        valores = [e.get(metrica) or 0 for e in esc]
        xs = [j + (i - (n - 1) / 2) * ancho for j in range(len(esc))]
        bs = ax.bar(xs, valores, ancho * 0.92, label=etiqueta, color=color)
        ax.bar_label(bs, fmt="%.1f", fontsize=8, padding=2)
    ax.set_xticks(range(len(escenarios)), [f"{u} usuarios" for u in escenarios])
    ax.set_title(titulo)
    ax.set_ylabel(unidad)


def main():
    datos = cargar()
    if not datos:
        raise SystemExit("No hay resultados en resultados/")
    salida = Path("informe/img")
    salida.mkdir(parents=True, exist_ok=True)

    graficas = [
        ("lat_p50_s", "Latencia p50", "segundos"),
        ("lat_p95_s", "Latencia p95", "segundos"),
        ("tok_s_agregado", "Tokens/s agregados (throughput)", "tokens/s"),
        ("throughput_req_s", "Peticiones completadas por segundo", "req/s"),
        ("tasa_error_pct", "Tasa de error", "%"),
        ("cpu_media_pct", "CPU media del sistema", "%"),
    ]
    for metrica, titulo, unidad in graficas:
        fig, ax = plt.subplots(figsize=(7, 3.6))
        barras(ax, datos, metrica, titulo, unidad)
        ax.legend(frameon=False, fontsize=8)
        fig.tight_layout()
        fig.savefig(salida / f"{metrica}.png", dpi=130)
        plt.close(fig)

    # Resumen: curva de latencia p95 frente a usuarios
    fig, ax = plt.subplots(figsize=(7, 3.6))
    for etiqueta, color, esc in datos.values():
        ax.plot([e["usuarios"] for e in esc], [e["lat_p95_s"] or 0 for e in esc], "o-", color=color,
                label=etiqueta, linewidth=2)
    ax.set_xlabel("usuarios simultáneos")
    ax.set_ylabel("segundos")
    ax.set_title("Degradación de la latencia p95 con la carga")
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(salida / "curva_p95.png", dpi=130)
    plt.close(fig)
    print(f"Gráficas en {salida}/")


if __name__ == "__main__":
    main()
