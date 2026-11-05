"""Creality Print desde fuera: instalación, perfiles, laminado por CLI y miniaturas.

Creality Print 7.x es un fork de Bambu Studio / OrcaSlicer y conserva su CLI, con
tres manías comprobadas en la 7.2.2:

- Las opciones van con guiones (`--load-settings`); con guion bajo dice
  "Invalid option".
- `--export-3mf` revienta al pintar las miniaturas, así que no se usa: las
  miniaturas del gcode las pone este módulo con `stl-thumb.exe`, que viene con
  Creality Print.
- Es una aplicación de ventana: no escribe nada útil por consola. Lo que pasa se
  sabe por el código de salida, `--logfile` y los ficheros que deja.

Desde la 7.3 hay que anteponer `--cli` y pedir el gcode con `--need-gcode-file`:
sin él lamina bien, sale con código 0 y borra el gcode. Con un proyecto 3MF, además,
no admite --load-settings: los perfiles se meten dentro del propio 3MF.
"""
from __future__ import annotations

import difflib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

import gcode

TIPOS = ("machine", "process", "filament")
SIN_VENTANA = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ErrorCrealityPrint(RuntimeError):
    pass


# ---------------------------------------------------------------- instalación

def _version_tupla(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", v)[:4])


def leer_conf(ruta: Path) -> dict:
    """`Creality.conf` es JSON seguido de una línea `# MD5 checksum ...`."""
    texto = ruta.read_text(encoding="utf-8")
    corte = texto.rfind("\n# MD5")
    return json.loads(texto[:corte] if corte > 0 else texto)


def _buscar_exe() -> Path:
    if os.environ.get("CREALITY_PRINT_EXE"):
        exe = Path(os.environ["CREALITY_PRINT_EXE"])
        if exe.is_file():
            return exe
        raise ErrorCrealityPrint(f"CREALITY_PRINT_EXE points to {exe}, which doesn't exist.")
    candidatos: list[Path] = []
    for base in {os.environ.get("ProgramFiles", r"C:\Program Files"), r"C:\Program Files"}:
        candidatos += Path(base, "Creality").glob("Creality Print*/CrealityPrint.exe")
    try:
        import winreg

        for raiz in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for sub in (r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
                        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"):
                try:
                    k = winreg.OpenKey(raiz, sub)
                except OSError:
                    continue
                for i in range(winreg.QueryInfoKey(k)[0]):
                    nombre = winreg.EnumKey(k, i)
                    if not nombre.lower().startswith("crealityprint"):
                        continue
                    try:
                        ubic = winreg.QueryValueEx(winreg.OpenKey(k, nombre), "InstallLocation")[0]
                    except OSError:
                        continue
                    exe = Path(ubic) / "CrealityPrint.exe"
                    if exe.is_file():
                        candidatos.append(exe)
    except ImportError:
        pass
    if not candidatos:
        raise ErrorCrealityPrint(
            "No encuentro Creality Print. Instálalo o define CREALITY_PRINT_EXE con la ruta a CrealityPrint.exe."
        )
    return max(set(candidatos), key=lambda p: _version_tupla(p.parent.name))


def _buscar_datos() -> Path:
    if os.environ.get("CREALITY_PRINT_DATA"):
        return Path(os.environ["CREALITY_PRINT_DATA"])
    base = Path(os.environ.get("APPDATA", "")) / "Creality" / "Creality Print"
    carpetas = [d for d in base.glob("*") if (d / "Creality.conf").is_file()]
    if not carpetas:
        raise ErrorCrealityPrint(f"Can't find Creality Print's data in {base}. Has it ever been opened?")
    return max(carpetas, key=lambda d: _version_tupla(d.name))


@dataclass
class CrealityPrint:
    exe: Path
    datos: Path

    @classmethod
    def detectar(cls) -> "CrealityPrint":
        return cls(exe=_buscar_exe(), datos=_buscar_datos())

    @property
    def conf(self) -> dict:
        return leer_conf(self.datos / "Creality.conf")

    @cached_property
    def version(self) -> str:
        """La del conf: Creality Print se actualiza solo y la carpeta de instalación no cambia de nombre."""
        try:
            return self.conf["app"]["version"]
        except (KeyError, OSError, ValueError):
            m = re.search(r"\d+(\.\d+)+", self.exe.parent.name)
            return m.group(0) if m else "0"

    @property
    def prefijo_cli(self) -> list[str]:
        return ["--cli"] if _version_tupla(self.version) >= (7, 3) else []

    @property
    def dir_sistema(self) -> Path:
        d = self.datos / "system" / "Creality"
        return d if d.is_dir() else self.exe.parent / "resources" / "profiles" / "Creality"

    @property
    def dirs_usuario(self) -> list[Path]:
        u = self.datos / "user"
        cuentas = sorted(d for d in u.glob("*") if d.is_dir() and d.name.isdigit())
        return cuentas + [u / "default"]

    @property
    def perfiles_activos(self) -> dict:
        """Lo que tiene seleccionado ahora mismo en la ventana."""
        p = self.conf.get("presets", {})
        return {"machine": p.get("machine"), "process": p.get("process"), "filaments": p.get("filaments") or []}

    @property
    def stl_thumb(self) -> Path | None:
        exe = self.exe.parent / "stl-thumb.exe"
        return exe if exe.is_file() else None

    def info(self) -> dict:
        return {
            "executable": str(self.exe),
            "version": self.version,
            "cli_mode": "7.3+ (--cli)" if self.prefijo_cli else "7.2 (Bambu)",
            "data_dir": str(self.datos),
            "system_profiles": str(self.dir_sistema),
            "user_profiles": [str(d) for d in self.dirs_usuario if d.is_dir()],
            "selected_profiles": self.perfiles_activos,
            "app_running": _proceso_abierto(),
        }

    # ------------------------------------------------------------ perfiles

    @cached_property
    def _indice(self) -> dict[str, dict[str, tuple[Path, str]]]:
        """tipo -> nombre -> (ruta, origen). Los de usuario tapan a los de sistema."""
        indice: dict[str, dict[str, tuple[Path, str]]] = {t: {} for t in TIPOS}
        for tipo in TIPOS:
            for f in (self.dir_sistema / tipo).glob("*.json"):
                indice[tipo][f.stem] = (f, "sistema")
            for d in reversed(self.dirs_usuario):
                for f in (d / tipo).glob("*.json"):
                    indice[tipo][f.stem] = (f, "usuario")
        return indice

    def _cargar(self, tipo: str, nombre: str) -> tuple[dict, str]:
        try:
            ruta, origen = self._indice[tipo][nombre]
        except KeyError:
            parecidos = difflib.get_close_matches(nombre, self._indice[tipo].keys(), n=5, cutoff=0.5)
            pista = f" Did you mean: {', '.join(parecidos)}?" if parecidos else ""
            raise ErrorCrealityPrint(f"There is no {tipo} profile '{nombre}'.{pista}") from None
        return json.loads(ruta.read_text(encoding="utf-8")), origen

    def listar_perfiles(self, tipo: str, filtro: str = "", maquina: str | None = None,
                        solo_seleccionables: bool = True) -> list[dict]:
        """Perfiles elegibles. Para proceso y filamento, solo los de `maquina`.

        Los nombres siguen la forma "Hyper PLA @Creality K2 0.4 nozzle": se filtra por
        "@<máquina>" porque "K2 0.4" también casa con "K2 Pro 0.4" y "K2 Plus 0.4".
        `maquina=""` quita el filtro.
        """
        if tipo not in TIPOS:
            raise ErrorCrealityPrint(f"Unknown profile kind '{tipo}': use {', '.join(TIPOS)}.")
        palabras = filtro.lower().split()
        if maquina is None and tipo != "machine":
            maquina = self.perfiles_activos["machine"] or ""
        sufijo = f"@{maquina}".lower() if maquina and tipo != "machine" else ""
        salida = []
        for nombre, (ruta, origen) in sorted(self._indice[tipo].items()):
            if palabras and not all(p in nombre.lower() for p in palabras):
                continue
            if sufijo and sufijo not in nombre.lower():
                continue
            if solo_seleccionables and origen == "sistema":
                try:
                    datos = json.loads(ruta.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if str(datos.get("instantiation", "true")).lower() != "true":
                    continue
            salida.append({"name": nombre, "source": "user" if origen == "usuario" else "system"})
        return salida

    def resolver_perfil(self, tipo: str, nombre: str) -> dict:
        """Aplana la cadena de `inherits` en un único diccionario.

        Los perfiles de usuario solo guardan lo que cambian ("inherits": "Hyper PLA
        @Creality K2 0.4 nozzle" y poco más), así que hay que resolverlos antes de
        pasárselos a la CLI.
        """
        cadena = []
        visto = set()
        actual = nombre
        while actual:
            if actual in visto:
                raise ErrorCrealityPrint(f"Circular inheritance in profile '{nombre}'.")
            visto.add(actual)
            datos, _ = self._cargar(tipo, actual)
            cadena.append(datos)
            actual = datos.get("inherits", "")
        plano: dict = {}
        for datos in reversed(cadena):
            plano.update(datos)
        plano["inherits"] = ""
        plano["name"] = nombre
        return plano


def _proceso_abierto() -> bool:
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq CrealityPrint.exe", "/NH"],
                           capture_output=True, text=True, timeout=10, creationflags=SIN_VENTANA)
        return "CrealityPrint.exe" in r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


# ---------------------------------------------------------------- ajustes

def _a_texto(valor) -> str:
    if isinstance(valor, bool):
        return "1" if valor else "0"
    return str(valor)


def aplicar_ajustes(perfiles: dict[str, list[dict]], ajustes: dict) -> list[str]:
    """Cambia claves de los perfiles ya aplanados. Devuelve dónde ha ido cada una.

    Una clave que no existe en ningún perfil es un error: la CLI la ignoraría sin
    avisar y el gcode saldría con el valor de siempre.
    """
    hechos = []
    todas = {k for lista in perfiles.values() for p in lista for k in p}
    for clave, valor in ajustes.items():
        destino = next((t for t in ("process", "filament", "machine")
                        if any(clave in p for p in perfiles[t])), None)
        if destino is None:
            parecidas = difflib.get_close_matches(clave, todas, n=5, cutoff=0.6)
            pista = f" Similar keys: {', '.join(parecidas)}." if parecidas else ""
            raise ErrorCrealityPrint(f"The setting '{clave}' doesn't exist in the profiles.{pista}")
        for p in perfiles[destino]:
            if clave not in p:
                continue
            actual = p[clave]
            if isinstance(actual, list):
                p[clave] = [_a_texto(v) for v in valor] if isinstance(valor, list) else [_a_texto(valor)] * len(actual)
            else:
                p[clave] = _a_texto(valor)
        hechos.append(f"{clave} = {valor!r} ({destino})")
    return hechos


# ---------------------------------------------------------------- 3MF

def info_3mf(ruta: Path) -> dict:
    """Qué perfiles y filamentos trae un proyecto 3MF (formato Bambu/Creality)."""
    with zipfile.ZipFile(ruta) as z:
        nombres = set(z.namelist())
        ajustes = {}
        if "Metadata/project_settings.config" in nombres:
            try:
                ajustes = json.loads(z.read("Metadata/project_settings.config"))
            except ValueError:
                ajustes = {}
        placas = sorted(n for n in nombres if re.fullmatch(r"Metadata/plate_\d+\.png", n))
        aplicacion = ""
        if "3D/3dmodel.model" in nombres:
            m = re.search(rb'name="Application">([^<]*)<', z.read("3D/3dmodel.model")[:20000])
            aplicacion = m.group(1).decode("utf-8", "replace") if m else ""
    filamentos = ajustes.get("filament_settings_id") or []
    if not filamentos:
        # 3MF sin datos de proyecto (p. ej. los que prepara preparar_multicolor): cuántos
        # extrusores piden sus objetos y sus cambios por altura.
        with zipfile.ZipFile(ruta) as z:
            textos = [z.read(n).decode("utf-8", "replace") for n in
                      ("Metadata/model_settings.config", "Metadata/custom_gcode_per_layer.xml",
                       "Metadata/layer_config_ranges.xml") if n in nombres]
        usados = [int(v) for t in textos for v in
                  re.findall(r'(?:key="extruder" value="|extruder="|opt_key="extruder">)(\d+)', t)]
        filamentos = [""] * max(usados, default=1)
    return {
        "aplicacion": aplicacion,
        "maquina": ajustes.get("printer_settings_id"),
        "proceso": ajustes.get("print_settings_id"),
        "filamentos": filamentos,
        "colores": ajustes.get("filament_colour") or [],
        "tipos": ajustes.get("filament_type") or [],
        "num_filamentos": max(1, len(filamentos)),
        "miniaturas_placa": placas,
    }


_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
    'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>'
)
_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
    '</Types>'
)
_RE_VERTICE = re.compile(r'<vertex\s+x="([^"]+)"\s+y="([^"]+)"\s+z="([^"]+)"')


def preparar_3mf_plano(ruta: Path, destino: Path, cama: tuple[float, float]) -> dict | None:
    """Adapta un 3MF "plano" (el de trimesh, Blender, un script…) para la CLI.

    Creality Print, al abrirlo en la ventana, centra el conjunto en la cama y deja
    elegir filamento por objeto. La CLI no hace ninguna de las dos cosas: si el
    modelo viene centrado en el origen, medio queda fuera y no lamina. Aquí se
    desplaza el conjunto entero (sin separar las piezas) al centro de la cama y,
    si los objetos usan `basematerials`, se escribe `Metadata/model_settings.config`
    con un extrusor por material.

    Devuelve None si el 3MF ya es un proyecto de Creality/Bambu (no hay que tocarlo).
    """
    with zipfile.ZipFile(ruta) as z:
        nombres = z.namelist()
        if "Metadata/model_settings.config" in nombres or "3D/3dmodel.model" not in nombres:
            return None
        xml = z.read("3D/3dmodel.model").decode("utf-8")
        otros = {n: z.read(n) for n in nombres if n != "3D/3dmodel.model"}
    if "<components>" in xml or re.search(r"<item [^>]*transform=", xml):
        return {"centrado": False, "aviso": "The 3MF uses components or transforms; it wasn't re-centred."}
    xs, ys, zs = [], [], []
    for m in _RE_VERTICE.finditer(xml):
        xs.append(float(m.group(1)))
        ys.append(float(m.group(2)))
        zs.append(float(m.group(3)))
    if not xs:
        return None
    dx = cama[0] / 2 - (min(xs) + max(xs)) / 2
    dy = cama[1] / 2 - (min(ys) + max(ys)) / 2
    dz = -min(zs) + 0.0  # + 0.0 quita el -0.0
    xml = re.sub(r'<item\s+objectid="(\d+)"\s*/>',
                 lambda m: f'<item objectid="{m.group(1)}" transform="1 0 0 0 1 0 0 0 1 {dx:.4f} {dy:.4f} {dz:.4f}"/>',
                 xml)
    materiales = re.findall(r'<base\s+name="([^"]*)"\s+displaycolor="#?([0-9A-Fa-f]{6})', xml)
    objetos = []
    for etiqueta in re.findall(r"<object\s[^>]*>", xml):
        atributos = dict(re.findall(r'(\w+)="([^"]*)"', etiqueta))
        objetos.append((atributos.get("id", ""), atributos.get("name", ""), atributos.get("pindex", "")))
    usados = sorted({int(p) for _, _, p in objetos if p})
    extrusor = {p: i + 1 for i, p in enumerate(usados)}
    config = ['<?xml version="1.0" encoding="UTF-8"?>', "<config>"]
    for oid, nombre, p in objetos:
        config += [f'  <object id="{oid}">', f'    <metadata key="name" value="{nombre}"/>',
                   f'    <metadata key="extruder" value="{extrusor.get(int(p), 1) if p else 1}"/>', "  </object>"]
    config.append("</config>")
    # Creality Print no abre un 3MF sin estos dos, y algunos generadores no los escriben.
    otros.setdefault("_rels/.rels", _RELS.encode())
    otros.setdefault("[Content_Types].xml", _CONTENT_TYPES.encode())
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as out:
        out.writestr("3D/3dmodel.model", xml)
        for n, datos in otros.items():
            out.writestr(n, datos)
        out.writestr("Metadata/model_settings.config", "\n".join(config) + "\n")
    colores = [f"#{materiales[p][1].upper()}" for p in usados if p < len(materiales)]
    return {
        "centrado": True,
        "desplazamiento_mm": [round(dx, 2), round(dy, 2), round(dz, 2)],
        "tamano_mm": [round(max(xs) - min(xs), 1), round(max(ys) - min(ys), 1), round(max(zs) - min(zs), 1)],
        "num_filamentos": max(1, len(usados)),
        "colores": colores,
        "materiales": [materiales[p][0] for p in usados if p < len(materiales)],
    }


# ---------------------------------------------------------------- multicolor

_CLAVES_DE_PERFIL = {"name", "inherits", "from", "setting_id", "instantiation", "version", "type", "filament_id"}


def meter_perfiles_en_3mf(origen: Path, destino: Path, planos: dict[str, list[dict]]) -> None:
    """Copia el 3MF con los perfiles aplanados dentro de `Metadata/project_settings.config`.

    Es lo que hace falta en la 7.3, que rechaza --load-settings/--load-filaments con un
    proyecto ("cannot be used when slicing a 3MF project"). Máquina y proceso van tal cual;
    cada clave de filamento se convierte en una lista con un valor por filamento.
    """
    with zipfile.ZipFile(origen) as z:
        nombres = z.namelist()
        ajustes = json.loads(z.read("Metadata/project_settings.config")) \
            if "Metadata/project_settings.config" in nombres else {}
        cama_vieja = _rectangulo(ajustes.get("printable_area"))
        cama_nueva = _rectangulo(planos["machine"][0].get("printable_area"))
        for tipo in ("machine", "process"):
            for k, v in planos[tipo][0].items():
                if k not in _CLAVES_DE_PERFIL:
                    ajustes[k] = v
        filamentos = planos["filament"]
        for k in {k for f in filamentos for k in f} - _CLAVES_DE_PERFIL:
            ajustes[k] = [(f[k][0] if isinstance(f.get(k), list) and f[k] else f.get(k, ""))
                          for f in filamentos]
        ajustes.update(printer_settings_id=planos["machine"][0]["name"],
                       print_settings_id=planos["process"][0]["name"],
                       filament_settings_id=[f["name"] for f in filamentos])
        # Un 3MF de otra máquina guarda de qué perfiles de sistema heredaba (proceso,
        # filamentos…, máquina). Si se queda, la CLI vuelve a cargar esa máquina (una K2 Pro,
        # cama de 300) por encima de lo que se acaba de meter: los perfiles van ya aplanados.
        ajustes["inherits_group"] = [""] * (len(filamentos) + 2)
        ajustes["print_compatible_printers"] = [planos["machine"][0]["name"]]
        mover = bool(cama_vieja and cama_nueva and cama_vieja != cama_nueva)
        if mover:
            _torre_dentro(ajustes, cama_vieja, cama_nueva)
        capa = _a_texto(planos["process"][0].get("layer_height", "")) or _a_texto(ajustes.get("layer_height", ""))
        with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as out:
            for info in z.infolist():
                if info.filename == "Metadata/project_settings.config":
                    continue
                datos = z.read(info.filename)
                if mover and info.filename == "3D/3dmodel.model":
                    datos = _recolocar_items(datos.decode("utf-8"), cama_vieja, cama_nueva).encode("utf-8")
                elif info.filename == "Metadata/layer_config_ranges.xml" and capa:
                    datos = _rangos_con_capa(datos.decode("utf-8"), capa).encode("utf-8")
                out.writestr(info, datos)
            out.writestr("Metadata/project_settings.config", json.dumps(ajustes, indent=4, ensure_ascii=False))


def _rectangulo(area) -> tuple[float, float, float, float] | None:
    """printable_area ("0x0,260x0,…" o lista) -> (xmin, ymin, xmax, ymax)."""
    if isinstance(area, str):
        area = area.split(",")
    try:
        puntos = [tuple(float(c) for c in p.split("x")) for p in area or []]
    except (ValueError, AttributeError):
        return None
    if len(puntos) < 3:
        return None
    xs, ys = [p[0] for p in puntos], [p[1] for p in puntos]
    return min(xs), min(ys), max(xs), max(ys)


# Creality Print pone las placas en rejilla, cada una a 1,2 veces el tamaño de la cama.
_PASO_PLACA = 1.2


def _recolocar_items(modelo: str, vieja: tuple, nueva: tuple) -> str:
    """Lleva cada objeto de la cama vieja a la nueva conservando su sitio respecto al centro.

    Un proyecto de una K2 Pro (cama de 300) trae la pieza centrada en 150,150; en una K2
    (260) el centro es 130,130 y, sin moverla, una pieza grande o la torre se salen.
    """
    def nuevo_valor(v: float, eje: int) -> float:
        a0, a1, b0, b1 = vieja[eje], vieja[eje + 2], nueva[eje], nueva[eje + 2]
        paso_a, paso_b = (a1 - a0) * _PASO_PLACA, (b1 - b0) * _PASO_PLACA
        placa = (v - a0) // paso_a  # en qué placa de la rejilla cae
        relativo = v - placa * paso_a - (a0 + a1) / 2
        return placa * paso_b + (b0 + b1) / 2 + relativo

    def cambiar(m: re.Match) -> str:
        nums = m.group(2).split()
        if len(nums) != 12:
            return m.group(0)
        nums[9] = f"{nuevo_valor(float(nums[9]), 0):.6g}"
        nums[10] = f"{nuevo_valor(float(nums[10]), 1):.6g}"
        return m.group(1) + " ".join(nums) + m.group(3)

    return re.sub(r'(<item\s[^>]*?transform=")([^"]*)(")', cambiar, modelo)


def _torre_dentro(ajustes: dict, vieja: tuple, nueva: tuple) -> None:
    """La torre de purga, en la misma proporción de la cama nueva y sin salirse."""
    try:
        ancho = float(_a_texto(ajustes.get("prime_tower_width", "35")) or 35)
    except ValueError:
        ancho = 35.0
    for clave, eje in (("wipe_tower_x", 0), ("wipe_tower_y", 1)):
        valores = ajustes.get(clave)
        if not valores:
            continue
        lista = valores if isinstance(valores, list) else [valores]
        salida = []
        for v in lista:
            try:
                f = (float(v) - vieja[eje]) / (vieja[eje + 2] - vieja[eje])
            except (ValueError, ZeroDivisionError):
                salida.append(v)
                continue
            n = nueva[eje] + f * (nueva[eje + 2] - nueva[eje])
            n = max(nueva[eje] + 5, min(n, nueva[eje + 2] - ancho - 5))
            salida.append(f"{n:.3f}")
        ajustes[clave] = salida if isinstance(valores, list) else salida[0]


def _rangos_con_capa(xml: str, capa: str) -> str:
    """Creality Print se cae (0xC0000005) si un rango de altura no lleva layer_height."""
    def completar(m: re.Match) -> str:
        if 'opt_key="layer_height"' in m.group(0):
            return m.group(0)
        return m.group(0).replace("</range>", f' <option opt_key="layer_height">{capa}</option>\n  </range>')
    return re.sub(r"<range [^>]*>.*?</range>", completar, xml, flags=re.S)


def stl_a_3mf(stl: Path, destino: Path) -> Path:
    """Un STL (binario o ASCII) como 3MF plano de un objeto, para poder darle colores."""
    import struct

    datos = stl.read_bytes()
    triangulos: list[tuple[tuple[float, float, float], ...]] = []
    es_binario = len(datos) >= 84 and 84 + 50 * struct.unpack_from("<I", datos, 80)[0] == len(datos)
    if es_binario:
        n = struct.unpack_from("<I", datos, 80)[0]
        for i in range(n):
            v = struct.unpack_from("<12f", datos, 84 + 50 * i)
            triangulos.append((v[3:6], v[6:9], v[9:12]))
    else:
        verts = [tuple(float(x) for x in m) for m in
                 re.findall(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", datos)]
        triangulos = [tuple(verts[i:i + 3]) for i in range(0, len(verts) - 2, 3)]
    if not triangulos:
        raise ErrorCrealityPrint(f"{stl.name} has no triangles: is it an STL?")
    indice: dict[tuple, int] = {}
    caras = []
    for t in triangulos:
        caras.append(tuple(indice.setdefault(tuple(round(c, 5) for c in p), len(indice)) for p in t))
    vertices = "".join(f'<vertex x="{x:.5f}" y="{y:.5f}" z="{z:.5f}"/>' for x, y, z in indice)
    tris = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in caras if len({a, b, c}) == 3)
    nombre = re.sub(r'[<>&"]', "_", stl.stem)
    modelo = (
        '<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><resources>'
        f'<object id="1" name="{nombre}" type="model"><mesh><vertices>{vertices}</vertices>'
        f'<triangles>{tris}</triangles></mesh></object></resources>'
        '<build><item objectid="1"/></build></model>'
    )
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("3D/3dmodel.model", modelo)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
    return destino


def objetos_3mf(ruta: Path) -> list[dict]:
    """Los objetos de un 3MF (los que se colocan en la cama) y su extrusor."""
    with zipfile.ZipFile(ruta) as z:
        nombres = set(z.namelist())
        modelo = z.read("3D/3dmodel.model").decode("utf-8", "replace")
        config = z.read("Metadata/model_settings.config").decode("utf-8", "replace") \
            if "Metadata/model_settings.config" in nombres else ""
    en_cama = re.findall(r'<item\s[^>]*objectid="(\d+)"', modelo)
    materiales = re.findall(r'<base\s+name="([^"]*)"', modelo)
    salida = []
    for etiqueta in re.findall(r"<object\s[^>]*>", modelo):
        a = dict(re.findall(r'(\w+)="([^"]*)"', etiqueta))
        if a.get("id") not in en_cama:
            continue
        bloque = re.search(rf'<object id="{a["id"]}">(.*?)</object>', config, re.S)
        nombre = a.get("name", "")
        extrusor = None
        if bloque:
            m = re.search(r'<metadata key="name" value="([^"]*)"', bloque.group(1))
            nombre = m.group(1) if m else nombre
            m = re.search(r'<metadata key="extruder" value="(\d+)"', bloque.group(1))
            extrusor = int(m.group(1)) if m else None
        pindex = a.get("pindex")
        salida.append({
            "id": a["id"],
            "name": nombre,
            "extruder": extrusor or 1,
            "material_3mf": materiales[int(pindex)] if pindex and int(pindex) < len(materiales) else None,
        })
    return salida


def _poner_extrusor(config: str, oid: str, nombre: str, extrusor: int) -> str:
    """Fija el extrusor de un objeto (y de sus partes) en model_settings.config."""
    patron = re.compile(rf'(<object id="{oid}">)(.*?)(</object>)', re.S)
    m = patron.search(config)
    if not m:
        bloque = (f'  <object id="{oid}">\n    <metadata key="name" value="{nombre}"/>\n'
                  f'    <metadata key="extruder" value="{extrusor}"/>\n  </object>\n')
        return config.replace("</config>", bloque + "</config>") if "</config>" in config else \
            f'<?xml version="1.0" encoding="UTF-8"?>\n<config>\n{bloque}</config>\n'
    cuerpo = re.sub(r'(<metadata key="extruder" value=")\d+(")', rf"\g<1>{extrusor}\2", m.group(2))
    cabeza = cuerpo.split("<part", 1)[0]
    if 'key="extruder"' not in cabeza:
        cuerpo = f'\n    <metadata key="extruder" value="{extrusor}"/>' + cuerpo
    return config[:m.start()] + m.group(1) + cuerpo + m.group(3) + config[m.end():]

_ALTURA_TOPE = 1000.0  # el último rango llega hasta aquí: más alto que cualquier impresora


def _leer_rangos(xml: str) -> dict[str, list[dict]]:
    """layer_config_ranges.xml -> {id de objeto: [{"min": z, "max": z, "opciones": {...}}]}."""
    rangos: dict[str, list[dict]] = {}
    for oid, cuerpo in re.findall(r'<object id="(\d+)">(.*?)</object>', xml, re.S):
        for zmin, zmax, ops in re.findall(r'<range min_z="([^"]+)" max_z="([^"]+)">(.*?)</range>', cuerpo, re.S):
            rangos.setdefault(oid, []).append({
                "min": float(zmin), "max": float(zmax),
                "opciones": dict(re.findall(r'<option opt_key="([^"]+)">([^<]*)</option>', ops))})
    return rangos


def _rangos_con_extrusor(existentes: list[dict], cambios: list[dict], extrusor_base: int) -> list[dict]:
    """Corta los rangos que ya hubiera por las alturas de cambio y pone el extrusor en cada tramo.

    Por debajo del primer cambio queda el extrusor del objeto; los demás ajustes de los rangos
    que ya había (p. ej. una altura de capa) se conservan en sus tramos.
    """
    cambios = sorted(cambios, key=lambda c: float(c["z"]))
    cortes = sorted({0.0, _ALTURA_TOPE} | {float(c["z"]) for c in cambios}
                    | {r["min"] for r in existentes} | {r["max"] for r in existentes})
    salida = []
    for zmin, zmax in zip(cortes, cortes[1:]):
        opciones = {}
        for r in existentes:
            if r["min"] <= zmin and zmax <= r["max"]:
                opciones.update(r["opciones"])
        extrusor = extrusor_base
        for c in cambios:
            if float(c["z"]) <= zmin:
                extrusor = c["extruder"]
        if opciones or extrusor != extrusor_base:
            # 0 = el del objeto; así el tramo base sigue al objeto si luego se le cambia.
            opciones["extruder"] = str(extrusor if extrusor != extrusor_base else 0)
            salida.append({"min": zmin, "max": zmax, "opciones": opciones})
    return salida


def _escribir_rangos(rangos: dict[str, list[dict]]) -> str:
    partes = ['<?xml version="1.0" encoding="utf-8"?>\n<objects>\n']
    for oid, lista in rangos.items():
        partes.append(f' <object id="{oid}">\n')
        for r in lista:
            partes.append(f'  <range min_z="{r["min"]:g}" max_z="{r["max"]:g}">\n')
            partes += [f'   <option opt_key="{k}">{v}</option>\n' for k, v in r["opciones"].items()]
            partes.append("  </range>\n")
        partes.append(" </object>\n")
    partes.append("</objects>\n")
    return "".join(partes)


# ---------------------------------------------------------------- relieves (HueForge, carteles)

_RE_TRIANGULO = re.compile(r'<triangle\s+v1="(\d+)"\s+v2="(\d+)"\s+v3="(\d+)"')
_IDENTIDAD = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)


def _matriz(texto: str | None) -> tuple[float, ...]:
    if not texto:
        return _IDENTIDAD
    nums = tuple(float(x) for x in texto.split())
    return nums if len(nums) == 12 else _IDENTIDAD


def _aplicar(m: tuple[float, ...], p: tuple[float, float, float]) -> tuple[float, float, float]:
    # 3MF: vector fila por la matriz; los tres últimos números son la traslación.
    x, y, z = p
    return (x * m[0] + y * m[3] + z * m[6] + m[9],
            x * m[1] + y * m[4] + z * m[7] + m[10],
            x * m[2] + y * m[5] + z * m[8] + m[11])


def _solo_traslacion(m: tuple[float, ...]) -> bool:
    return all(abs(a - b) < 1e-6 for a, b in zip(m[:9], _IDENTIDAD[:9]))


def _bloque_objeto(xml: str, oid: str) -> re.Match | None:
    return re.search(rf'(<object\s[^>]*\bid="{oid}"[^>]*>)(.*?)(</object>)', xml, re.S)


def _malla_de(xml: str, oid: str) -> tuple[list, list] | None:
    m = _bloque_objeto(xml, oid)
    if not m or "<mesh" not in m.group(2):
        return None
    cuerpo = m.group(2)
    vertices = [(float(a), float(b), float(c)) for a, b, c in _RE_VERTICE.findall(cuerpo)]
    triangulos = [(int(a), int(b), int(c)) for a, b, c in _RE_TRIANGULO.findall(cuerpo)]
    return vertices, triangulos


def _geometria_objeto(contenido: dict[str, bytes], oid: str) -> dict | None:
    """Triángulos de un objeto en sus coordenadas propias y en las de la cama.

    Sirve para objetos con malla directa (3MF plano) y para los proyectos de Creality/Bambu,
    cuyo objeto son componentes que apuntan a mallas en 3D/Objects/*.model.
    """
    raiz = contenido["3D/3dmodel.model"].decode("utf-8", "replace")
    m = _bloque_objeto(raiz, oid)
    if not m:
        return None
    piezas = []  # (vertices, triangulos, matriz del componente)
    if "<mesh" in m.group(2):
        malla = _malla_de(raiz, oid)
        if malla:
            piezas.append((*malla, _IDENTIDAD))
    for etiqueta in re.findall(r"<component\s[^>]*/?>", m.group(2)):
        a = dict(re.findall(r'([\w:]+)="([^"]*)"', etiqueta))
        ruta = a.get("p:path", "").lstrip("/")
        xml = contenido.get(ruta, b"").decode("utf-8", "replace") if ruta else raiz
        malla = _malla_de(xml, a.get("objectid", ""))
        if malla:
            piezas.append((*malla, _matriz(a.get("transform"))))
    if not piezas:
        return None
    item = re.search(rf'<item\s[^>]*objectid="{oid}"[^>]*>', raiz)
    t = re.search(r'transform="([^"]*)"', item.group(0)) if item else None
    t_item = _matriz(t.group(1) if t else None)
    locales, cama = [], []
    for vertices, triangulos, tc in piezas:
        vl = [_aplicar(tc, p) for p in vertices]
        vc = [_aplicar(t_item, p) for p in vl]
        locales += [(vl[a], vl[b], vl[c]) for a, b, c in triangulos]
        cama += [(vc[a], vc[b], vc[c]) for a, b, c in triangulos]
    return {"local": locales, "cama": cama, "item": t_item}


def _area_xy(t) -> float:
    """Área proyectada en XY con signo (>0 mira hacia arriba)."""
    (x0, y0, _), (x1, y1, _), (x2, y2, _) = t
    return ((x1 - x0) * (y2 - y0) - (y1 - y0) * (x2 - x0)) / 2


def superficies_por_altura(triangulos: list) -> list[dict]:
    """Las caras planas que miran hacia arriba, agrupadas por altura: [{"z", "area_mm2"}]."""
    areas: dict[float, float] = {}
    for t in triangulos:
        zs = (t[0][2], t[1][2], t[2][2])
        if max(zs) - min(zs) < 1e-4:
            a = _area_xy(t)
            if a > 0:
                z = round(zs[0], 2) + 0.0
                areas[z] = areas.get(z, 0.0) + a
    return [{"z": z, "area_mm2": round(a, 1)} for z, a in sorted(areas.items())]


def altura_de_cambio(superficies: list[dict]) -> float | None:
    """Para un relieve de dos colores (HueForge, cartel): la cara alta más baja que importe.

    Lo que queda a esa altura o por debajo es el fondo; el color de arriba empieza en la capa
    siguiente. Si se cambia más arriba, el color de arriba queda como una piel fina y desvaída.
    """
    total = sum(s["area_mm2"] for s in superficies)
    tope = max((s["z"] for s in superficies), default=None)
    bajas = [s["z"] for s in superficies if s["z"] != tope and s["area_mm2"] >= 0.05 * total]
    return min(bajas) if bajas else None


def _superficies_objeto(contenido: dict[str, bytes], oid: str) -> list[dict] | None:
    g = _geometria_objeto(contenido, oid)
    if not g:
        return None
    zmin = min(p[2] for t in g["cama"] for p in t)
    return superficies_por_altura([tuple((x, y, z - zmin) for x, y, z in t) for t in g["cama"]])


def analizar_alturas(modelo: str | os.PathLike) -> list[dict]:
    """Por objeto: sus caras de arriba por altura y dónde cambiar de color en un relieve."""
    modelo = Path(modelo).resolve()
    if not modelo.is_file():
        raise ErrorCrealityPrint(f"{modelo} doesn't exist.")
    with tempfile.TemporaryDirectory() as tmp:
        origen = stl_a_3mf(modelo, Path(tmp) / "m.3mf") if modelo.suffix.lower() == ".stl" else modelo
        with zipfile.ZipFile(origen) as z:
            contenido = {n: z.read(n) for n in z.namelist()}
        objetos = objetos_3mf(origen)
    salida = []
    for o in objetos:
        sup = _superficies_objeto(contenido, o["id"])
        if sup is not None:
            salida.append({"id": o["id"], "name": o["name"], "top_surfaces": sup,
                           "suggested_change_z": altura_de_cambio(sup)})
    return salida


def _caja(x0, y0, z0, x1, y1, z1) -> tuple[list, list]:
    v = [(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
    caras = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1),
             (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]
    return v, caras


def _xml_malla(oid: int, vertices: list, triangulos: list) -> str:
    vs = "".join(f'<vertex x="{x:.5f}" y="{y:.5f}" z="{z:.5f}"/>' for x, y, z in vertices)
    ts = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in triangulos)
    return (f'<object id="{oid}" type="model"><mesh><vertices>{vs}</vertices>'
            f'<triangles>{ts}</triangles></mesh></object>')


def _ids(xml: str) -> list[int]:
    return [int(i) for i in re.findall(r'<object\s[^>]*\bid="(\d+)"', xml)]


def partes_por_altura(contenido: dict[str, bytes], o: dict, cambios: list[dict]) -> str | None:
    """Pone el cambio de color por altura como partes, para que la app lo pinte en «Preparar».

    La malla del objeto queda entera con el filamento de arriba y, por cada cambio, se añade
    un bloque macizo desde la cama hasta esa altura con el filamento de debajo. Los bloques
    van de arriba abajo porque Creality Print, donde dos partes se solapan, se queda con la
    que va después. Solo se hace si el objeto apoya en un rectángulo (HueForge, carteles,
    litofanías): con otra planta el bloque añadiría material. Devuelve un aviso o None.
    """
    g = _geometria_objeto(contenido, o["id"])
    if not g:
        return f"{o['name']}: couldn't read its mesh; the colour change is a height range only."
    if not _solo_traslacion(g["item"]):
        return f"{o['name']}: it's rotated or scaled; the colour change is a height range only."
    pts = [p for t in g["local"] for p in t]
    x0, x1 = min(p[0] for p in pts), max(p[0] for p in pts)
    y0, y1 = min(p[1] for p in pts), max(p[1] for p in pts)
    z0 = min(p[2] for p in pts)
    planta = -sum(_area_xy(t) for t in g["local"] if all(abs(p[2] - z0) < 1e-4 for p in t))
    if planta < 0.98 * (x1 - x0) * (y1 - y0):
        return (f"{o['name']}: it doesn't sit on a rectangle, so the Prepare view shows one colour "
                "(it still prints right; Preview shows both).")
    dz = g["item"][11]  # altura sobre la cama -> coordenadas del objeto
    cambios = sorted(cambios, key=lambda c: float(c["z"]))
    base = o["extruder"] or 1
    debajo = [base] + [c["extruder"] for c in cambios[:-1]]  # filamento bajo cada cambio
    arriba = cambios[-1]["extruder"]
    # +0,01: el bloque asoma por encima de las caras del fondo, que están justo a esa altura,
    # y así la vista no las mezcla; a 0,01 mm no llega ninguna capa.
    bloques = [(_caja(x0, y0, z0, x1, y1, float(c["z"]) - dz + 0.01), e, float(c["z"]))
               for c, e in reversed(list(zip(cambios, debajo)))]

    raiz = contenido["3D/3dmodel.model"].decode("utf-8")
    m = _bloque_objeto(raiz, o["id"])
    partes, pieza_original = [], None
    if "<components>" in m.group(2):
        # Proyecto de Creality/Bambu: los bloques van al fichero de las mallas.
        primero = re.search(r'<component\s[^>]*p:path="([^"]+)"', m.group(2))
        if not primero:
            return f"{o['name']}: unexpected 3MF layout; the colour change is a height range only."
        ruta = primero.group(1)
        sub = contenido[ruta.lstrip("/")].decode("utf-8")
        siguiente = max(_ids(sub) + _ids(raiz)) + 1
        nuevos, componentes = [], []
        for (v, t), e, z in bloques:
            nuevos.append(_xml_malla(siguiente, v, t))
            componentes.append(f'<component p:path="{ruta}" objectid="{siguiente}" '
                               'transform="1 0 0 0 1 0 0 0 1 0 0 0"/>')
            partes.append((siguiente, f"Below {z:g} mm", e))
            siguiente += 1
        contenido[ruta.lstrip("/")] = sub.replace("</resources>", "".join(nuevos) + "</resources>", 1).encode()
        cuerpo = m.group(2).replace("</components>", "".join(componentes) + "</components>", 1)
    else:
        # 3MF plano: la malla pasa a un objeto propio y el objeto se vuelve componentes.
        pieza_original = max(_ids(raiz)) + 1
        etiqueta = re.sub(r'\bid="\d+"', f'id="{pieza_original}"', m.group(1), count=1)
        etiqueta = re.sub(r'\s(?:pid|pindex)="[^"]*"', "", etiqueta)
        nuevos = [etiqueta + m.group(2) + m.group(3)]
        componentes = [f'<component objectid="{pieza_original}"/>']
        siguiente = pieza_original + 1
        for (v, t), e, z in bloques:
            nuevos.append(_xml_malla(siguiente, v, t))
            componentes.append(f'<component objectid="{siguiente}"/>')
            partes.append((siguiente, f"Below {z:g} mm", e))
            siguiente += 1
        cuerpo = "<components>" + "".join(componentes) + "</components>"
        raiz = raiz.replace("</resources>", "".join(nuevos) + "</resources>", 1)
        m = _bloque_objeto(raiz, o["id"])
    etiqueta = re.sub(r'\s(?:pid|pindex)="[^"]*"', "", m.group(1))
    raiz = raiz[:m.start()] + etiqueta + cuerpo + m.group(3) + raiz[m.end():]
    contenido["3D/3dmodel.model"] = raiz.encode("utf-8")

    # model_settings: las piezas de siempre con el filamento de arriba y los bloques después.
    config = contenido.get("Metadata/model_settings.config", b"").decode("utf-8") or \
        '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n</config>\n'
    config = _poner_extrusor(config, o["id"], o["name"], base)
    mo = re.search(rf'(<object id="{o["id"]}">)(.*?)(</object>)', config, re.S)
    cuerpo = mo.group(2).rstrip()
    if pieza_original is not None and "<part " not in cuerpo:
        cuerpo += (f'\n    <part id="{pieza_original}" subtype="normal_part">\n'
                   f'      <metadata key="name" value="{o["name"]}"/>\n    </part>')

    def con_extrusor(p: re.Match) -> str:
        dentro = re.sub(r'\s*<metadata key="extruder" value="\d+"/>', "", p.group(2)).rstrip()
        return f'{p.group(1)}{dentro}\n      <metadata key="extruder" value="{arriba}"/>\n    {p.group(3)}'

    cuerpo = re.sub(r"(<part\b[^>]*>)(.*?)(</part>)", con_extrusor, cuerpo, flags=re.S)
    for pid, nombre, e in partes:
        cuerpo += (f'\n    <part id="{pid}" subtype="normal_part">\n'
                   f'      <metadata key="name" value="{nombre}"/>\n'
                   f'      <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>\n'
                   f'      <metadata key="extruder" value="{e}"/>\n    </part>')
    config = config[:mo.start()] + mo.group(1) + cuerpo + "\n  " + mo.group(3) + config[mo.end():]
    contenido["Metadata/model_settings.config"] = config.encode("utf-8")
    return None


def preparar_multicolor(
    modelo: str | os.PathLike,
    asignar: dict[str, int] | None = None,
    cambios_altura: list[dict] | None = None,
    salida: str | os.PathLike | None = None,
    cama: tuple[float, float] = (260, 260),
    partes: bool = True,
) -> dict:
    """Prepara un 3MF con los colores que se piden, listo para `laminar`.

    - asignar: {nombre o id del objeto: extrusor}. Los extrusores son 1, 2… (T0, T1…) y,
      si se definen `mezclas` al laminar, las mezclas son los siguientes números.
    - cambios_altura: [{"z": 10, "extrusor": 2}] cambia de filamento a esa altura (base de
      un color y relieve de otro, como en los carteles). "z": "auto" la mide en cada objeto:
      justo encima del fondo de un relieve (HueForge).
    - partes: además del rango de altura, un bloque con el filamento de abajo para que la
      vista «Preparar» lo pinte (solo en objetos que apoyan en un rectángulo).

    Un STL se convierte antes en 3MF; un 3MF plano se centra en la cama. El original no se toca.
    """
    modelo = Path(modelo).resolve()
    if not modelo.is_file():
        raise ErrorCrealityPrint(f"{modelo} doesn't exist.")
    destino = Path(salida).resolve() if salida else modelo.with_name(f"{modelo.stem}_multicolor.3mf")
    with tempfile.TemporaryDirectory() as tmp:
        origen = modelo
        if modelo.suffix.lower() == ".stl":
            origen = stl_a_3mf(modelo, Path(tmp) / "desde_stl.3mf")
        elif modelo.suffix.lower() != ".3mf":
            raise ErrorCrealityPrint("prepare_multicolor accepts STL and 3MF.")
        centrado = Path(tmp) / "centrado.3mf"
        if preparar_3mf_plano(origen, centrado, cama) and centrado.is_file():
            origen = centrado
        objetos = objetos_3mf(origen)
        with zipfile.ZipFile(origen) as z:
            contenido = {n: z.read(n) for n in z.namelist()}

    config = contenido.get("Metadata/model_settings.config", b"").decode("utf-8")
    if not config:
        config = '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n</config>\n'
    for clave, extrusor in (asignar or {}).items():
        if int(extrusor) < 1:
            raise ErrorCrealityPrint("Extruders start at 1.")
        o = next((o for o in objetos if clave == o["id"] or clave.lower() == o["name"].lower()), None)
        if o is None:
            raise ErrorCrealityPrint(
                f"There is no object '{clave}'. Objects: " + ", ".join(f"{o['name']} ({o['id']})" for o in objetos))
        config = _poner_extrusor(config, o["id"], o["name"], int(extrusor))
        o["extruder"] = int(extrusor)
    contenido["Metadata/model_settings.config"] = config.encode("utf-8")

    cambios_altura = [{"z": c["z"], "extruder": int(c.get("extruder", c.get("extrusor", 0)))}
                      for c in cambios_altura or []]
    avisos: list[str] = []
    por_objeto: dict[str, list[dict]] = {}
    if cambios_altura:
        if any(c["extruder"] < 1 for c in cambios_altura):
            raise ErrorCrealityPrint("Extruders start at 1.")
        for o in objetos:
            lista = []
            for c in cambios_altura:
                z = c["z"]
                if isinstance(z, str) and z.strip().lower() in ("auto", "automatico", "automático"):
                    sup = _superficies_objeto(contenido, o["id"]) or []
                    z = altura_de_cambio(sup)
                    if z is None:
                        raise ErrorCrealityPrint(
                            f"{o['name']}: no low (background) surfaces found for z='auto'; give the "
                            "height in mm. Top surfaces: " + ", ".join(f"{s['z']} mm" for s in sup))
                lista.append({"z": float(z), "extruder": c["extruder"]})
            por_objeto[o["id"]] = lista
        # El cambio va como modificador de rango de altura (como «Añadir rango de altura» en la
        # app), no como cambio de herramienta por capa en modo «MultiAsSingle». Con eso se
        # imprime bien, pero «Preparar» sigue pintando el objeto de un color: por eso, además,
        # se ponen partes con su filamento cuando la forma lo permite (partes_por_altura).
        rangos = _leer_rangos(contenido.get("Metadata/layer_config_ranges.xml", b"").decode("utf-8"))
        for i, o in enumerate(objetos, start=1):
            rangos[str(i)] = _rangos_con_extrusor(rangos.get(str(i), []), por_objeto[o["id"]],
                                                  o["extruder"] or 1)
        if partes:
            for o in objetos:
                aviso = partes_por_altura(contenido, o, por_objeto[o["id"]])
                if aviso:
                    avisos.append(aviso)
        xml_rangos = _escribir_rangos(rangos)
        try:
            capa = json.loads(contenido.get("Metadata/project_settings.config", b"{}")).get("layer_height")
        except ValueError:
            capa = None
        if capa:  # sin proyecto (un STL), `laminar` pone la del proceso que se use
            xml_rangos = _rangos_con_capa(xml_rangos, _a_texto(capa))
        contenido["Metadata/layer_config_ranges.xml"] = xml_rangos.encode("utf-8")
        # Los cambios de herramienta que trajera el proyecto chocarían con los rangos.
        xml = contenido.get("Metadata/custom_gcode_per_layer.xml", b"").decode("utf-8")
        if xml:
            contenido["Metadata/custom_gcode_per_layer.xml"] = re.sub(
                r'\s*<layer [^>]*gcode="tool_change"[^>]*/>', "", xml).encode("utf-8")

    destino.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in contenido.items():
            z.writestr(n, d)
    objetos = objetos_3mf(destino)
    usados = sorted({o["extruder"] for o in objetos} | {c["extruder"] for c in cambios_altura})
    resultado = {"file": str(destino), "objects": objetos, "extruders_used": usados}
    if por_objeto:
        resultado["height_changes"] = [{"object": o["name"], "changes": por_objeto[o["id"]]}
                                       for o in objetos if o["id"] in por_objeto]
    if avisos:
        resultado["warnings"] = avisos
    return resultado


MODOS_MEZCLA = {"layers": 0, "dots": 1, "simple": 2, "capas": 0, "puntos": 1}


def filas_mezcla(mezclas: list[dict], n_fisicos: int) -> str:
    """[{"a": 1, "b": 2, "percent_b": 50, "mode": "layers"}] -> mixed_filament_definitions.

    Formato de MixedFilamentManager::serialize_custom_entries (Creality Print 7.2):
    a,b,activo,custom,%B,0,g,w,m<modo>,d0,o0,u<id>. La mezcla i es el extrusor n_fisicos+i.
    """
    filas = []
    for i, m in enumerate(mezclas, start=1):
        a, b = int(m.get("a", 1)), int(m.get("b", 2))
        if not (1 <= a <= n_fisicos and 1 <= b <= n_fisicos) or a == b:
            raise ErrorCrealityPrint(f"Mix {i}: a and b must be two different filaments between 1 and {n_fisicos}.")
        modo = m.get("mode", m.get("modo", "layers"))
        if modo not in MODOS_MEZCLA:
            raise ErrorCrealityPrint(f"Mix {i}: invalid mode '{modo}'; use layers, dots or simple.")
        pct = max(0, min(100, int(m.get("percent_b", m.get("porcentaje_b", 50)))))
        filas.append(f"{a},{b},1,1,{pct},0,g,w,m{MODOS_MEZCLA[modo]},d0,o0,u{i}")
    return ";".join(filas)


# ---------------------------------------------------------------- miniaturas

def _tamanos(maquina: dict) -> list[tuple[int, int]]:
    valor = maquina.get("thumbnails") or "96x96/PNG, 300x300/PNG"
    if isinstance(valor, list):
        valor = ",".join(valor)
    tams = [(int(a), int(b)) for a, b in re.findall(r"(\d+)x(\d+)", valor)]
    return tams or [(96, 96), (300, 300)]


def _png_escalado(png: bytes, ancho: int, alto: int) -> bytes:
    from PIL import Image

    img = Image.open(io.BytesIO(png)).convert("RGBA")
    img.thumbnail((ancho, alto), Image.LANCZOS)
    lienzo = Image.new("RGBA", (ancho, alto), (0, 0, 0, 0))
    lienzo.paste(img, ((ancho - img.width) // 2, (alto - img.height) // 2), img)
    out = io.BytesIO()
    lienzo.save(out, "PNG", optimize=True)
    return out.getvalue()


def _render_stl(stl_thumb: Path, modelo: Path, color: str) -> bytes | None:
    color = (color or "#F2754E").lstrip("#")[-6:]
    try:
        r = int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)
    except ValueError:
        r = (0xF2, 0x75, 0x4E)
    ambiente = "".join(f"{int(c * 0.45):02x}" for c in r)
    difuso = "".join(f"{c:02x}" for c in r)
    with tempfile.TemporaryDirectory() as tmp:
        salida = Path(tmp) / "m.png"
        try:
            subprocess.run([str(stl_thumb), "-s", "600", "-m", ambiente, difuso, "ffffff",
                            str(modelo), str(salida)],
                           capture_output=True, timeout=120, creationflags=SIN_VENTANA)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return salida.read_bytes() if salida.is_file() and salida.stat().st_size else None


def miniatura_base(cp: CrealityPrint, modelo: Path, color: str, placa: int) -> bytes | None:
    """PNG grande del que salen todas las miniaturas: la de la placa si es un 3MF, si no un render."""
    if modelo.suffix.lower() == ".3mf":
        try:
            with zipfile.ZipFile(modelo) as z:
                nombre = f"Metadata/plate_{placa}.png"
                if nombre in z.namelist():
                    return z.read(nombre)
        except zipfile.BadZipFile:
            return None
    if cp.stl_thumb and modelo.suffix.lower() in (".stl", ".3mf", ".obj"):
        return _render_stl(cp.stl_thumb, modelo, color)
    return None


# ---------------------------------------------------------------- laminar

def _nombre_tiempo(t: str) -> str:
    """'7h 57m 45s' -> '7h57m45s', como nombra Creality Print sus gcode."""
    return re.sub(r"\s+", "", t) or "0s"


def _nombre_seguro(texto: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", texto).strip() or "modelo"


@dataclass
class Laminado:
    gcodes: list[dict] = field(default_factory=list)
    perfiles: dict = field(default_factory=dict)
    ajustes_aplicados: list[str] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)
    segundos: float = 0.0
    log: str | None = None


def laminar(
    cp: CrealityPrint,
    modelo: str | os.PathLike,
    maquina: str | None = None,
    proceso: str | None = None,
    filamentos: list[str] | None = None,
    salida: str | os.PathLike | None = None,
    ajustes: dict | None = None,
    placa: int = 0,
    usar_perfiles_del_3mf: bool = False,
    colocar: bool = False,
    orientar: bool = False,
    miniaturas: bool = True,
    modelo_esperado: str | None = None,
    cama: tuple[float, float, float] | None = None,
    tiempo_max: int = 1800,
    mezclas: list[dict] | None = None,
) -> Laminado:
    """Lamina un STL/3MF/OBJ/STEP con la CLI de Creality Print y revisa el resultado.

    Por defecto usa los perfiles que están seleccionados en la ventana de Creality
    Print. Con un 3MF también se imponen esos perfiles salvo que se pida
    `usar_perfiles_del_3mf`: un 3MF descargado trae la máquina de quien lo subió
    (una K1C, una K2 Pro…) y la CLI la obedece sin rechistar.

    `mezclas` crea filamentos virtuales que alternan dos físicos (ver filas_mezcla): con N
    filamentos, la mezcla 1 es el extrusor N+1. Se asignan a objetos con preparar_multicolor.
    """
    modelo = Path(modelo).resolve()
    if not modelo.is_file():
        raise ErrorCrealityPrint(f"{modelo} doesn't exist.")
    es_3mf = modelo.suffix.lower() == ".3mf"
    salida = Path(salida).resolve() if salida else modelo.parent
    salida.mkdir(parents=True, exist_ok=True)
    res = Laminado()
    activos = cp.perfiles_activos
    proyecto = info_3mf(modelo) if es_3mf else None

    trabajo = Path(tempfile.mkdtemp(prefix="cp_laminar_", dir=salida))
    log = trabajo / "creality_print.log"
    # La 7.3 lamina pero borra el gcode al acabar si no se le pide con --need-gcode-file.
    pide_gcode = ["--need-gcode-file"] if cp.prefijo_cli else []
    cmd = [str(cp.exe), *cp.prefijo_cli, *pide_gcode, "--slice", str(placa), "--outputdir", str(trabajo),
           "--logfile", str(log), "--debug", "3"]
    if es_3mf:
        cmd.append("--allow-newer-file")

    preparado = None
    if es_3mf and not usar_perfiles_del_3mf:
        ancho, fondo = _cama_de(cp, maquina or activos["machine"], cama)
        preparado = preparar_3mf_plano(modelo, trabajo / "modelo.3mf", (ancho, fondo))
        if preparado and preparado.get("aviso"):
            res.avisos.append(preparado["aviso"])
        elif preparado:
            res.avisos.append(
                f"3MF without Creality data: centred on the bed "
                f"({preparado['tamano_mm'][0]}×{preparado['tamano_mm'][1]} mm), "
                f"{preparado['num_filamentos']} filament(s) by material: {', '.join(preparado['materiales']) or '—'}."
            )
            proyecto = {**proyecto, "num_filamentos": preparado["num_filamentos"], "colores": preparado["colores"]}

    if es_3mf and usar_perfiles_del_3mf:
        if maquina or proceso or filamentos or ajustes:
            raise ErrorCrealityPrint("With use_3mf_profiles you can't pass profiles or settings.")
        res.perfiles = {"source": "3MF", "machine": proyecto["maquina"], "process": proyecto["proceso"],
                        "filaments": proyecto["filamentos"]}
    else:
        maquina = maquina or activos["machine"]
        proceso = proceso or activos["process"]
        n = proyecto["num_filamentos"] if proyecto else 1
        filamentos = list(filamentos or activos["filaments"][:1] or [])
        if mezclas:
            # Con mezclas, los extrusores por encima de los físicos son virtuales.
            if len(filamentos) < 2:
                raise ErrorCrealityPrint("Mixing needs at least two filaments in `filaments`.")
            n = len(filamentos)
        if not (maquina and proceso and filamentos):
            raise ErrorCrealityPrint("Profiles are missing and Creality Print has none selected.")
        if len(filamentos) < n:
            if len(filamentos) == 1:
                res.avisos.append(f"The 3MF uses {n} filaments: '{filamentos[0]}' is used for all of them.")
                filamentos = filamentos * n
            else:
                raise ErrorCrealityPrint(f"The 3MF uses {n} filaments and {len(filamentos)} were given.")
        planos = {
            "machine": [cp.resolver_perfil("machine", maquina)],
            "process": [cp.resolver_perfil("process", proceso)],
            "filament": [cp.resolver_perfil("filament", f) for f in filamentos],
        }
        modelo_perfil = planos["machine"][0].get("printer_model", "")
        if modelo_esperado and modelo_perfil and modelo_perfil != modelo_esperado:
            raise ErrorCrealityPrint(
                f"The machine profile '{maquina}' is for a '{modelo_perfil}' and the printer is a "
                f"'{modelo_esperado}'. Pick a '{modelo_esperado}' profile."
            )
        _comprobar_compatibles(planos, maquina, res.avisos)
        if proyecto:
            # Los colores del proyecto se conservan: el CFS los usa para casar ranuras.
            for i, p in enumerate(planos["filament"]):
                if i < len(proyecto["colores"]) and proyecto["colores"][i]:
                    p["filament_colour"] = [proyecto["colores"][i]]
        if ajustes:
            res.ajustes_aplicados = aplicar_ajustes(planos, ajustes)
        if mezclas:
            planos["process"][0]["mixed_filament_definitions"] = filas_mezcla(mezclas, len(filamentos))
            res.ajustes_aplicados.append(
                "mixes: " + ", ".join(f"extruder {len(filamentos) + i} = {m.get('a', 1)}+{m.get('b', 2)} "
                                      f"({m.get('mode', m.get('modo', 'layers'))}, "
                                      f"{m.get('percent_b', m.get('porcentaje_b', 50))} % of {m.get('b', 2)})"
                                        for i, m in enumerate(mezclas, start=1)))
        if es_3mf and cp.prefijo_cli:
            # La 7.3 no admite --load-settings con un proyecto 3MF: los perfiles van dentro.
            origen = trabajo / "modelo.3mf" if preparado and preparado.get("centrado") else modelo
            meter_perfiles_en_3mf(origen, trabajo / "perfiles.3mf", planos)
            preparado = {"centrado": True}
            os.replace(trabajo / "perfiles.3mf", trabajo / "modelo.3mf")
        else:
            rutas = {}
            for tipo, lista in planos.items():
                rutas[tipo] = []
                for i, p in enumerate(lista):
                    f = trabajo / f"{tipo}_{i}.json"
                    f.write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")
                    rutas[tipo].append(str(f))
            cmd += ["--load-settings", ";".join(rutas["machine"] + rutas["process"]),
                    "--load-filaments", ";".join(rutas["filament"])]
        res.perfiles = {"source": "given", "machine": maquina, "process": proceso, "filaments": filamentos}
    if colocar:
        cmd += ["--arrange", "1"]
    if orientar:
        cmd += ["--orient", "1"]
    cmd.append(str(trabajo / "modelo.3mf") if preparado and preparado.get("centrado") else str(modelo))

    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=tiempo_max, creationflags=SIN_VENTANA)
        codigo = proc.returncode
    except subprocess.TimeoutExpired:
        codigo = None
    res.segundos = round(time.monotonic() - t0, 1)
    generados = sorted(trabajo.glob("plate_*.gcode"), key=lambda p: int(re.findall(r"\d+", p.stem)[0]))

    if codigo != 0 or not generados:
        destino_log = salida / f"{_nombre_seguro(modelo.stem)}_slicing_error.log"
        if log.is_file():
            shutil.copyfile(log, destino_log)
        motivo = _motivo_error(log) or (f"took longer than {tiempo_max} s" if codigo is None else f"exit code {codigo}")
        shutil.rmtree(trabajo, ignore_errors=True)
        raise ErrorCrealityPrint(f"Creality Print couldn't slice {modelo.name}: {motivo}. Log: {destino_log}")

    try:
        maquina_plana = cp.resolver_perfil("machine", res.perfiles["machine"]) if res.perfiles.get("machine") else {}
    except ErrorCrealityPrint:
        maquina_plana = {}
    for g in generados:
        n_placa = int(re.findall(r"\d+", g.stem)[0])
        inf = gcode.analizar(g, modelo_esperado, cama)
        if miniaturas and not inf.miniaturas:
            colores = inf.lista("filament_colour")
            base = miniatura_base(cp, modelo, colores[0] if colores else "", n_placa)
            if base:
                gcode.insertar_miniaturas(g, [(w, h, _png_escalado(base, w, h)) for w, h in _tamanos(maquina_plana)])
                inf = gcode.analizar(g, modelo_esperado, cama)
            else:
                res.avisos.append(f"Plate {n_placa}: couldn't render the thumbnail.")
        # El material del nombre es el que usa esta placa, no el primero del proyecto.
        tipos_proyecto = inf.lista("filament_type") or ["PLA"]
        tipos_usados = [tipos_proyecto[t] for t in inf.herramientas if t < len(tipos_proyecto)]
        tipo = "-".join(dict.fromkeys(tipos_usados)) or tipos_proyecto[0]
        nombre = f"{_nombre_seguro(modelo.name)}_{tipo}_{_nombre_tiempo(inf.tiempo_estimado)}"
        if len(generados) > 1:
            nombre += f"_plate{n_placa}"
        destino = salida / f"{nombre}.gcode"
        os.replace(g, destino)
        inf.ruta = str(destino)
        res.gcodes.append({"plate": n_placa, **inf.resumen()})
    shutil.rmtree(trabajo, ignore_errors=True)
    return res


def _cama_de(cp: CrealityPrint, maquina: str | None, cama: tuple | None) -> tuple[float, float]:
    if cama:
        return float(cama[0]), float(cama[1])
    if maquina:
        caja = gcode._cama({"printable_area": ",".join(cp.resolver_perfil("machine", maquina).get("printable_area", []))})
        if caja:
            return caja[1] - caja[0], caja[3] - caja[2]
    return 260.0, 260.0


def _comprobar_compatibles(planos: dict, maquina: str, avisos: list[str]) -> None:
    for tipo in ("process", "filament"):
        for p in planos[tipo]:
            compatibles = p.get("compatible_printers") or []
            if compatibles and maquina not in compatibles:
                avisos.append(f"Profile '{p.get('name')}' doesn't list '{maquina}' as compatible.")


# Lo que Creality Print escribe en el log y lo que significa para quien lamina.
_EXPLICACIONES = [
    (r"gcode path conflicts found between (.+?) and (.+)",
     "the toolpaths of '{0}' and '{1}' collide (often the prime tower doesn't fit: try "
     "settings={{'enable_prime_tower': 0}} or move the parts apart in Creality Print)"),
    (r"found slicing result conflict", "toolpaths of different objects collide"),
    (r"outside of plate|out of (the )?plate|outside the print area", "the model goes outside the bed"),
    (r"File Version .* not supported", "the 3MF comes from another Creality Print version"),
    (r"got error when validate: (.+)", "incompatible settings: {0}"),
]


def _motivo_error(log: Path) -> str | None:
    if not log.is_file():
        return None
    texto = log.read_text(encoding="utf-8", errors="replace")
    for patron, explicacion in _EXPLICACIONES:
        m = re.search(patron, texto)
        if m:
            return explicacion.format(*[g for g in m.groups() if g is not None])
    lineas = texto.splitlines()
    malas = [l for l in lineas if re.search(r"\[error\]|failed|invalid", l, re.I)
             and "crealityprint_main start" not in l]
    if not malas:
        return None
    ultima = malas[-1]
    return re.sub(r"^\[[^\]]*\]\s*\[[^\]]*\]\s*\[[^\]]*\]\s*", "", ultima).strip()[:400]


# ---------------------------------------------------------------- abrir en la ventana

def abrir(cp: CrealityPrint, ruta: str | os.PathLike, en_ventana_abierta: bool = False) -> str:
    """Abre un modelo, proyecto o gcode en Creality Print para revisarlo a ojo.

    Por defecto abre una ventana nueva: meterlo en la que ya está abierta añade el
    modelo al proyecto que tengas a medias.
    """
    ruta = Path(ruta).resolve()
    if not ruta.is_file():
        raise ErrorCrealityPrint(f"{ruta} doesn't exist.")
    cmd = [str(cp.exe)]
    if en_ventana_abierta:
        cmd.append("--single-instance")
    cmd.append(str(ruta))
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(cmd, creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return "in the open window" if en_ventana_abierta else "in a new window"
