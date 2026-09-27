"""La impresora Creality: Moonraker (7125) para leer y subir, y el websocket de
Creality OS (9999) para lanzar trabajos, igual que hace Creality Print.

Vale para las Creality con Creality OS (Klipper + Moonraker + el servicio del 9999):
la familia K2 y la K1, entre otras. Probado en una K2 con CFS.

Por qué el websocket y no `POST /printer/print/start` de Moonraker: al lanzar por
Moonraker, una K2 con CFS reutiliza el mapeo de ranuras del trabajo anterior (o pide la
ranura A), y ese mapeo no se puede cambiar desde la API de Moonraker. Creality Print lo
resuelve mandando por el 9999 primero `colorMatch` (qué extrusor del gcode sale de qué
ranura) y después `multiColorPrint`; sin CFS, `opGcodeFile`. Este módulo hace lo mismo.
Sacado de resources/web/deviceMgr de Creality Print 7.2.2 (DeviceInterface).
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import httpx
from websockets.sync.client import connect as ws_connect

PUERTO_MOONRAKER = 7125
PUERTO_WS = 9999
# Si Moonraker no dice dónde guarda los gcode (/server/files/roots), el de la K2.
DIR_GCODES_POR_DEFECTO = "/mnt/UDISK/printer_data/gcodes"

# Por si Creality Print no está: los modelos más comunes con Creality OS.
_MODELOS_BASE = {
    "F021": ("Creality K2", (260, 260, 260)),
    "F012": ("Creality K2 Pro", (300, 300, 300)),
    "F008": ("Creality K2 Plus", (350, 350, 350)),
    "F016": ("Creality K2 SE", (220, 215, 245)),
    "CR-K1": ("Creality K1", (220, 220, 250)),
    "CR-K1 Max": ("Creality K1 Max", (300, 300, 300)),
    "K1C": ("Creality K1C", (220, 220, 250)),
}


def _cargar_modelos() -> dict[str, tuple[str, tuple[int, int, int]]]:
    """Código que manda la impresora -> (printer_model de los perfiles, cama).

    Sale de la lista de impresoras de Creality Print (system/Creality/machineList.json),
    que conoce todas las suyas. El código es el que la impresora da por el websocket
    ("model": "F021" en una K2) y el que Creality Print guarda en deviceInfo.json.
    """
    modelos = dict(_MODELOS_BASE)
    base = Path(os.environ.get("APPDATA", "")) / "Creality" / "Creality Print"
    for lista in sorted(base.glob("*/system/Creality/machineList.json"), reverse=True):
        try:
            impresoras = json.loads(lista.read_text(encoding="utf-8"))["printerList"]
        except (OSError, ValueError, KeyError):
            continue
        conocidos = set()
        for f in (lista.parent / "machine").glob("*.json"):
            try:
                m = json.loads(f.read_text(encoding="utf-8")).get("printer_model")
            except (OSError, ValueError):
                continue
            if m:
                conocidos.add(m)
        for p in impresoras:
            codigo, nombre = p.get("printerIntName"), p.get("name", "")
            if not codigo or codigo in modelos or not p.get("xSize"):
                continue
            opciones = [f"Creality {nombre}", f"Creality {nombre.split('_')[0]}", nombre]
            printer_model = next((o for o in opciones if o in conocidos), opciones[0])
            modelos[codigo] = (printer_model, (int(p["xSize"]), int(p["ySize"]), int(p.get("zSize") or 0)))
        break
    return modelos


MODELOS = _cargar_modelos()

# Estados de print_stats en los que la impresora está libre.
LIBRE = {"standby", "complete", "cancelled", "error"}


class ErrorImpresora(RuntimeError):
    pass


def _hosts_de_creality_print() -> list[tuple[str, str]]:
    """(ip, modelo) de las impresoras que Creality Print tiene guardadas."""
    base = Path(os.environ.get("APPDATA", "")) / "Creality" / "Creality Print"
    salida = []
    for f in sorted(base.glob("*/deviceInfo.json"), reverse=True):
        try:
            datos = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for grupo in datos.get("groups", []):
            for d in grupo.get("list", []) or []:
                if d.get("address"):
                    salida.append((d["address"], d.get("model", "")))
    return salida


def ranura_a_indices(ranura: str) -> tuple[int, int]:
    """'1D' -> (caja 1, material 3). También vale 'T1D' o 'd' (caja 1)."""
    r = ranura.strip().upper().removeprefix("T")
    if len(r) == 1:
        r = "1" + r
    if len(r) != 2 or not r[0].isdigit() or r[1] not in "ABCD":
        raise ErrorImpresora(f"Invalid slot '{ranura}': use 1A, 1B, 1C, 1D (2A… with more CFS units).")
    return int(r[0]), "ABCD".index(r[1])


def id_extrusor(extrusor: int) -> str:
    """Extrusor 1-based del gcode -> id que espera colorMatch (1 -> T1A, 5 -> T2A).

    Es la fórmula de Creality Print: `T${floor((e-1)/4+1)}${'A'+(e-1)%4}`.
    """
    e = extrusor - 1
    return f"T{e // 4 + 1}{'ABCD'[e % 4]}"


@dataclass
class Impresora:
    host: str
    modelo_codigo: str = ""
    _dir_gcodes: str | None = None

    @classmethod
    def detectar(cls) -> "Impresora":
        """Dónde está la impresora, por este orden:

        1. La variable CREALITY_HOST (o K2_HOST, el nombre antiguo).
        2. La que guardó `configurar_impresora` en la configuración del plugin.
        3. Las que tiene guardadas Creality Print.
        """
        host = os.environ.get("CREALITY_HOST") or os.environ.get("K2_HOST")
        if host:
            return cls(host, os.environ.get("CREALITY_MODELO") or os.environ.get("K2_MODELO", ""))
        import config  # aquí para no importar en círculo

        candidatos = []
        guardada = config.impresora_guardada()
        if guardada:
            candidatos.append(guardada)
        candidatos += [c for c in _hosts_de_creality_print() if c[0] != (guardada or ("",))[0]]
        for host, modelo in candidatos:
            try:
                httpx.get(f"http://{host}:{PUERTO_MOONRAKER}/server/info", timeout=3).raise_for_status()
                return cls(host, modelo)
            except httpx.HTTPError:
                continue
        probadas = f" (tried: {', '.join(h for h, _ in candidatos)})" if candidatos else ""
        raise ErrorImpresora(
            f"Can't find the printer{probadas}. Use setup_printer to look for it on the network "
            "(its IP may have changed) or set CREALITY_HOST."
        )

    # ------------------------------------------------------------ Moonraker

    @property
    def _url(self) -> str:
        return f"http://{self.host}:{PUERTO_MOONRAKER}"

    def _get(self, ruta: str, **params) -> dict:
        try:
            r = httpx.get(self._url + ruta, params=params or None, timeout=15)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise ErrorImpresora(f"Moonraker isn't answering at {self.host}: {e}") from e
        return r.json()["result"]

    def consultar(self, *objetos: str) -> dict:
        consulta = "&".join(urllib.parse.quote(o) for o in objetos)
        try:
            r = httpx.get(f"{self._url}/printer/objects/query?{consulta}", timeout=15)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise ErrorImpresora(f"Moonraker isn't answering at {self.host}: {e}") from e
        return r.json()["result"]["status"]

    @property
    def dir_gcodes(self) -> str:
        """Dónde guarda la impresora los gcode (en la K2 /mnt/UDISK/…, en la K1 /usr/data/…)."""
        if self._dir_gcodes is None:
            try:
                raices = self._get("/server/files/roots")
                self._dir_gcodes = next(r["path"] for r in raices if r.get("name") == "gcodes")
            except (ErrorImpresora, StopIteration, KeyError, TypeError):
                self._dir_gcodes = DIR_GCODES_POR_DEFECTO
        return self._dir_gcodes.rstrip("/")

    @property
    def modelo(self) -> tuple[str, tuple[int, int, int]] | None:
        if not self.modelo_codigo:
            # Con K2_HOST no se sabe el modelo: se lo pregunta a la impresora.
            try:
                self.modelo_codigo = self.estado_ws().get("model") or ""
            except ErrorImpresora:
                return None
        return MODELOS.get(self.modelo_codigo)

    def estado(self) -> dict:
        s = self.consultar("print_stats", "display_status", "virtual_sdcard", "heater_bed", "extruder",
                           "temperature_sensor chamber_temp", "webhooks", "bed_mesh")
        ps, vs = s.get("print_stats", {}), s.get("virtual_sdcard", {})
        datos = (vs.get("cur_print_data") or {}).get("metadata") or {}
        # El de virtual_sdcard es el que enseña la pantalla; display_status se queda atrás.
        progreso = vs.get("progress") or s.get("display_status", {}).get("progress") or 0
        restante = None
        if ps.get("state") == "printing" and datos.get("estimated_time") and progreso:
            restante = max(0, int(datos["estimated_time"] * (1 - progreso)))
        return {
            "printer": self.host,
            "model": self.modelo[0] if self.modelo else self.modelo_codigo or None,
            "klipper": s.get("webhooks", {}).get("state"),
            "state": ps.get("state"),
            "file": ps.get("filename") or None,
            "progress_pct": round(progreso * 100, 1),
            "layer": vs.get("layer"),
            "layers": vs.get("layer_count"),
            "printed_for": _hms(ps.get("print_duration")),
            "time_left_approx": _hms(restante),
            "filament_used_m": round((ps.get("filament_used") or 0) / 1000, 2),
            "nozzle": _temp(s.get("extruder")),
            "bed": _temp(s.get("heater_bed")),
            "chamber": round(s.get("temperature_sensor chamber_temp", {}).get("temperature", 0), 1),
            "bed_mesh": s.get("bed_mesh", {}).get("profile_name"),
            "message": ps.get("message") or None,
        }

    def archivos(self, filtro: str = "", limite: int = 20) -> list[dict]:
        lista = self._get("/server/files/list", root="gcodes")
        palabras = filtro.lower().split()
        lista = [f for f in lista if all(p in f["path"].lower() for p in palabras)]
        lista.sort(key=lambda f: -f.get("modified", 0))
        return [{"name": f["path"], "mb": round(f["size"] / 1e6, 2),
                 "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(f["modified"]))}
                for f in lista[:limite]]

    def historial(self, limite: int = 10) -> list[dict]:
        trabajos = self._get("/server/history/list", limit=limite, order="desc").get("jobs", [])
        return [{"file": j.get("filename"), "status": j.get("status"),
                 "started": time.strftime("%Y-%m-%d %H:%M", time.localtime(j.get("start_time", 0))),
                 "duration": _hms(j.get("total_duration")),
                 "filament_m": round((j.get("filament_used") or 0) / 1000, 2)} for j in trabajos]

    def metadatos(self, nombre: str) -> dict:
        return self._get("/server/files/metadata", filename=nombre)

    def descargar(self, nombre: str, destino: Path) -> Path:
        url = f"{self._url}/server/files/gcodes/{urllib.parse.quote(nombre)}"
        with httpx.stream("GET", url, timeout=120) as r:
            if r.status_code == 404:
                raise ErrorImpresora(f"The printer has no file '{nombre}'.")
            r.raise_for_status()
            with open(destino, "wb") as f:
                for trozo in r.iter_bytes(1 << 20):
                    f.write(trozo)
        return destino

    def subir(self, local: str | os.PathLike, nombre: str | None = None) -> dict:
        local = Path(local)
        nombre = nombre or local.name
        if not nombre.lower().endswith(".gcode"):
            raise ErrorImpresora("Only .gcode files can be uploaded.")
        estado = self.consultar("print_stats")["print_stats"]
        if estado.get("state") in ("printing", "paused") and estado.get("filename") == nombre:
            raise ErrorImpresora(f"'{nombre}' is printing right now; upload it under another name.")
        with open(local, "rb") as f:
            try:
                r = httpx.post(f"{self._url}/server/files/upload", data={"root": "gcodes"},
                               files={"file": (nombre, f, "application/octet-stream")}, timeout=600)
                r.raise_for_status()
            except httpx.HTTPError as e:
                raise ErrorImpresora(f"Upload failed: {e}") from e
        return {"name": nombre, "path_on_printer": f"{self.dir_gcodes}/{nombre}",
                "mb": round(local.stat().st_size / 1e6, 2)}

    # ------------------------------------------------------------ websocket 9999

    def _ws(self, mensajes: list[dict], esperar: float = 3.0, hasta=None) -> dict:
        """Manda mensajes por el 9999 y junta lo que conteste durante `esperar` segundos.

        La impresora manda {"ModeCode":"heart_beat"} y espera un "ok" de vuelta; si no se
        contesta, corta. Devuelve la última versión de cada clave recibida.
        """
        visto: dict = {}
        try:
            with ws_connect(f"ws://{self.host}:{PUERTO_WS}", open_timeout=5, max_size=None) as ws:
                for m in mensajes:
                    ws.send(json.dumps(m))
                fin = time.monotonic() + esperar
                while time.monotonic() < fin:
                    try:
                        crudo = ws.recv(timeout=max(0.1, fin - time.monotonic()))
                    except TimeoutError:
                        break
                    try:
                        d = json.loads(crudo)
                    except ValueError:
                        continue
                    if isinstance(d, dict) and d.get("ModeCode") == "heart_beat":
                        ws.send("ok")
                        continue
                    if isinstance(d, dict):
                        visto.update(d)
                        if hasta and hasta(visto):
                            break
        except OSError as e:
            raise ErrorImpresora(f"The printer's websocket ({self.host}:{PUERTO_WS}) isn't answering: {e}") from e
        return visto

    def cfs(self) -> dict:
        d = self._ws([{"method": "get", "params": {"boxsInfo": 1}}], esperar=4,
                     hasta=lambda v: "boxsInfo" in v)
        info = d.get("boxsInfo")
        if not info:
            raise ErrorImpresora("The printer didn't send the CFS status.")
        ranuras = []
        for caja in info.get("materialBoxs", []):
            if caja.get("type") != 0:  # type 1 = portabobinas externo
                continue
            for m in caja.get("materials", []):
                ranuras.append({
                    "slot": f"{caja['id']}{'ABCD'[m['id']]}",
                    "material": m.get("type") or None,
                    "name": m.get("name") or None,
                    "brand": m.get("vendor") or None,
                    "colour": _color(m.get("color")),
                    "rfid": m.get("rfid") or None,
                    "remaining_pct": m.get("percent"),
                    "empty": m.get("state") != 1,
                    "in_use": bool(m.get("selected")),
                    "_color_crudo": m.get("color"),
                })
        return {
            "cfs_connected": bool(d.get("cfsConnect", 1)),
            "slots": ranuras,
            "last_job_mapping": [_mapeo_legible(m) for m in info.get("colorMatch") or []],
            "humidity_pct": next((c.get("humidity") for c in info.get("materialBoxs", []) if c.get("type") == 0), None),
        }

    def estado_ws(self) -> dict:
        d = self._ws([], esperar=3, hasta=lambda v: "deviceState" in v and "state" in v)
        return {k: d.get(k) for k in ("state", "deviceState", "printFileName", "model", "hostname", "cfsConnect",
                                      "printProgress", "err")}

    def imprimir(self, nombre: str, ranuras: dict[int, str] | None, tipos: list[str] | None = None,
                 calibracion: bool = False) -> dict:
        """Lanza `nombre` (ya subido) como lo lanza Creality Print.

        `ranuras` = {extrusor del gcode (1-based): "1A".."1D"}. Con ranuras se manda
        colorMatch + multiColorPrint; sin ellas, opGcodeFile (portabobinas externo).
        """
        ruta = f"{self.dir_gcodes}/{nombre}"
        mensajes = []
        mapeo = []
        if ranuras:
            cfs = self.cfs()
            por_ranura = {r["slot"]: r for r in cfs["slots"]}
            for extrusor, ranura in sorted(ranuras.items()):
                caja, material = ranura_a_indices(ranura)
                clave = f"{caja}{'ABCD'[material]}"
                r = por_ranura.get(clave)
                if r is None:
                    raise ErrorImpresora(f"Slot {clave} doesn't exist in the CFS.")
                if r["empty"]:
                    raise ErrorImpresora(f"Slot {clave} is empty.")
                tipo = (tipos[extrusor - 1] if tipos and extrusor - 1 < len(tipos) else None) or r["material"] or "PLA"
                mapeo.append({"id": id_extrusor(extrusor), "type": tipo, "color": r["_color_crudo"],
                              "boxId": caja, "materialId": material})
            mensajes.append({"method": "set", "params": {"colorMatch": {"path": ruta, "list": mapeo}}})
            mensajes.append({"method": "set", "params": {"multiColorPrint": {"gcode": ruta,
                                                                             "enableSelfTest": int(calibracion)}}})
        else:
            mensajes.append({"method": "set", "params": {"opGcodeFile": f"printprt:{ruta}",
                                                         "enableSelfTest": int(calibracion)}})
        respuesta = self._ws(mensajes, esperar=4)
        # Comprobar que ha arrancado de verdad: la impresora tarda unos segundos.
        arrancada = False
        for _ in range(15):
            ps = self.consultar("print_stats")["print_stats"]
            if ps.get("filename") == nombre and ps.get("state") in ("printing", "paused"):
                arrancada = True
                break
            time.sleep(2)
        return {"file": nombre, "mapping_sent": mapeo or None, "started": arrancada,
                "printer_reply": {k: respuesta[k] for k in ("err", "deviceState", "printFileName") if k in respuesta}}

    def control(self, accion: str) -> dict:
        """pausar / reanudar / cancelar, con los mismos mensajes que Creality Print."""
        params = {"pause": {"pause": 1}, "resume": {"pause": 0}, "cancel": {"stop": 1}}.get(accion)
        if params is None:
            raise ErrorImpresora("Invalid action: pause, resume or cancel.")
        self._ws([{"method": "set", "params": params}], esperar=2)
        time.sleep(2)
        return self.estado()


def _mapeo_legible(m: dict) -> dict:
    """{"id": "T1A", "boxId": 1, "materialId": 3} -> extrusor 1 del gcode sale de la ranura 1D.

    El "T1A" de colorMatch NO es una ranura: es el extrusor 1 escrito como Creality.
    """
    ident = m.get("id", "")
    try:
        extrusor = (int(ident[1]) - 1) * 4 + "ABCD".index(ident[2]) + 1
    except (ValueError, IndexError):
        extrusor = None
    ranura = f"{m.get('boxId')}{'ABCD'[m['materialId']]}" if isinstance(m.get("materialId"), int) else None
    return {"extruder": extrusor, "slot": ranura}


def _color(c: str | None) -> str | None:
    """Creality manda '#0ffffff' (un 0 delante): se queda en '#FFFFFF'."""
    if not c:
        return None
    c = c.lstrip("#")
    return "#" + c[-6:].upper() if len(c) >= 6 else None


def _temp(h: dict | None) -> str | None:
    if not h:
        return None
    return f"{h.get('temperature', 0):.0f}/{h.get('target', 0):.0f} °C"


def _hms(segundos) -> str | None:
    if segundos is None:
        return None
    s = int(segundos)
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"
