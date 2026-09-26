"""Lo personal de cada instalación: qué impresora es.

Vive fuera del repo, en %APPDATA%\\creality-print-plugin\\config.json (o donde diga
CREALITY_PLUGIN_CONFIG), y lo rellena el propio plugin buscando la impresora. Las reglas
y preferencias van aparte, en los CREALITY.md (ver preconfig.py). Nada de esto se publica.
"""
from __future__ import annotations

import concurrent.futures
import ipaddress
import json
import os
import socket
from pathlib import Path

import httpx

import k2 as k2m


def ruta() -> Path:
    if os.environ.get("CREALITY_PLUGIN_CONFIG"):
        return Path(os.environ["CREALITY_PLUGIN_CONFIG"])
    return Path(os.environ.get("APPDATA", Path.home())) / "creality-print-plugin" / "config.json"


def leer() -> dict:
    try:
        return json.loads(ruta().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def guardar(datos: dict) -> None:
    r = ruta()
    r.parent.mkdir(parents=True, exist_ok=True)
    tmp = r.with_suffix(".tmp")
    tmp.write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, r)


# ---------------------------------------------------------------- impresora

def impresora_guardada() -> tuple[str, str] | None:
    imp = leer().get("impresora") or {}
    return (imp["host"], imp.get("modelo_codigo", "")) if imp.get("host") else None


def guardar_impresora(host: str, modelo_codigo: str, nombre: str = "") -> dict:
    datos = leer()
    datos["impresora"] = {"host": host, "modelo_codigo": modelo_codigo, "nombre": nombre}
    guardar(datos)
    return datos["impresora"]


def identificar(host: str) -> dict | None:
    """¿Hay una Creality con Moonraker y el websocket 9999 en `host`? Devuelve qué es."""
    try:
        info = httpx.get(f"http://{host}:{k2m.PUERTO_MOONRAKER}/printer/info", timeout=2).json()["result"]
    except (httpx.HTTPError, ValueError, KeyError):
        return None
    impresora = k2m.K2(host)
    try:
        ws = impresora.estado_ws()
    except k2m.ErrorImpresora:
        ws = {}
    codigo = ws.get("model") or ""
    modelo = k2m.MODELOS.get(codigo)
    return {
        "host": host,
        "nombre": info.get("hostname") or ws.get("hostname") or "",
        "modelo_codigo": codigo,
        "modelo": modelo[0] if modelo else None,
        "cama_mm": modelo[1] if modelo else None,
        "soportada": modelo is not None,
    }


def _redes_locales() -> list[ipaddress.IPv4Network]:
    """Las /24 de las IP privadas de este equipo."""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:  # la IP de salida, por si gethostname no la da
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    redes = {ipaddress.ip_network(f"{ip}/24", strict=False) for ip in ips
             if ipaddress.ip_address(ip).is_private and not ip.startswith("127.")}
    return sorted(redes, key=str)


def _puerto_abierto(ip: str, puerto: int) -> bool:
    try:
        with socket.create_connection((ip, puerto), timeout=0.4):
            return True
    except OSError:
        return False


def buscar_impresoras() -> list[dict]:
    """Las de Creality Print primero; si no hay ninguna viva, escanea la red local (/24)."""
    vistos: dict[str, dict] = {}
    for host, _ in k2m._hosts_de_creality_print():
        r = identificar(host)
        if r:
            r["origen"] = "Creality Print"
            vistos[host] = r
    if vistos:
        return list(vistos.values())
    candidatos = [str(ip) for red in _redes_locales() for ip in red.hosts()]
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
        abiertos = [ip for ip, ok in zip(candidatos, ex.map(lambda i: _puerto_abierto(i, k2m.PUERTO_WS), candidatos))
                    if ok]
    for ip in abiertos:
        r = identificar(ip)
        if r:
            r["origen"] = "red local"
            vistos[ip] = r
    return list(vistos.values())
