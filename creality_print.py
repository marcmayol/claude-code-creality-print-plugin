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

Desde la 7.3 hay que anteponer `--cli`.
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
        raise ErrorCrealityPrint(f"CREALITY_PRINT_EXE apunta a {exe}, que no existe.")
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
        raise ErrorCrealityPrint(f"No encuentro los datos de Creality Print en {base}. ¿Lo has abierto alguna vez?")
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
            "ejecutable": str(self.exe),
            "version": self.version,
            "modo_cli": "7.3+ (--cli)" if self.prefijo_cli else "7.2 (Bambu)",
            "datos": str(self.datos),
            "perfiles_sistema": str(self.dir_sistema),
            "perfiles_usuario": [str(d) for d in self.dirs_usuario if d.is_dir()],
            "perfiles_activos": self.perfiles_activos,
            "abierto": _proceso_abierto(),
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
            pista = f" ¿Querías decir: {', '.join(parecidos)}?" if parecidos else ""
            raise ErrorCrealityPrint(f"No existe el perfil de {tipo} «{nombre}».{pista}") from None
        return json.loads(ruta.read_text(encoding="utf-8")), origen

    def listar_perfiles(self, tipo: str, filtro: str = "", maquina: str | None = None,
                        solo_seleccionables: bool = True) -> list[dict]:
        """Perfiles elegibles. Para proceso y filamento, solo los de `maquina`.

        Los nombres siguen la forma "Hyper PLA @Creality K2 0.4 nozzle": se filtra por
        "@<máquina>" porque "K2 0.4" también casa con "K2 Pro 0.4" y "K2 Plus 0.4".
        `maquina=""` quita el filtro.
        """
        if tipo not in TIPOS:
            raise ErrorCrealityPrint(f"Tipo de perfil «{tipo}» desconocido: usa {', '.join(TIPOS)}.")
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
            salida.append({"nombre": nombre, "origen": origen})
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
                raise ErrorCrealityPrint(f"Herencia circular en el perfil «{nombre}».")
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
            pista = f" Parecidas: {', '.join(parecidas)}." if parecidas else ""
            raise ErrorCrealityPrint(f"El ajuste «{clave}» no existe en los perfiles.{pista}")
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
        return {"centrado": False, "aviso": "El 3MF usa componentes o transformaciones; no lo he recolocado."}
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
) -> Laminado:
    """Lamina un STL/3MF/OBJ/STEP con la CLI de Creality Print y revisa el resultado.

    Por defecto usa los perfiles que están seleccionados en la ventana de Creality
    Print. Con un 3MF también se imponen esos perfiles salvo que se pida
    `usar_perfiles_del_3mf`: un 3MF descargado trae la máquina de quien lo subió
    (una K1C, una K2 Pro…) y la CLI la obedece sin rechistar.
    """
    modelo = Path(modelo).resolve()
    if not modelo.is_file():
        raise ErrorCrealityPrint(f"No existe {modelo}.")
    es_3mf = modelo.suffix.lower() == ".3mf"
    salida = Path(salida).resolve() if salida else modelo.parent
    salida.mkdir(parents=True, exist_ok=True)
    res = Laminado()
    activos = cp.perfiles_activos
    proyecto = info_3mf(modelo) if es_3mf else None

    trabajo = Path(tempfile.mkdtemp(prefix="cp_laminar_", dir=salida))
    log = trabajo / "creality_print.log"
    cmd = [str(cp.exe), *cp.prefijo_cli, "--slice", str(placa), "--outputdir", str(trabajo),
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
                f"3MF sin datos de Creality: recolocado en el centro de la cama "
                f"({preparado['tamano_mm'][0]}×{preparado['tamano_mm'][1]} mm) y "
                f"{preparado['num_filamentos']} filamento(s) por material: {', '.join(preparado['materiales']) or '—'}."
            )
            proyecto = {**proyecto, "num_filamentos": preparado["num_filamentos"], "colores": preparado["colores"]}

    if es_3mf and usar_perfiles_del_3mf:
        if maquina or proceso or filamentos or ajustes:
            raise ErrorCrealityPrint("Con usar_perfiles_del_3mf no se pueden pasar perfiles ni ajustes.")
        res.perfiles = {"origen": "3MF", **{k: proyecto[k] for k in ("maquina", "proceso", "filamentos")}}
    else:
        maquina = maquina or activos["machine"]
        proceso = proceso or activos["process"]
        n = proyecto["num_filamentos"] if proyecto else 1
        filamentos = list(filamentos or activos["filaments"][:1] or [])
        if not (maquina and proceso and filamentos):
            raise ErrorCrealityPrint("Faltan perfiles y Creality Print no tiene ninguno seleccionado.")
        if len(filamentos) < n:
            if len(filamentos) == 1:
                res.avisos.append(f"El 3MF usa {n} filamentos: se usa «{filamentos[0]}» para todos.")
                filamentos = filamentos * n
            else:
                raise ErrorCrealityPrint(f"El 3MF usa {n} filamentos y se han indicado {len(filamentos)}.")
        planos = {
            "machine": [cp.resolver_perfil("machine", maquina)],
            "process": [cp.resolver_perfil("process", proceso)],
            "filament": [cp.resolver_perfil("filament", f) for f in filamentos],
        }
        modelo_perfil = planos["machine"][0].get("printer_model", "")
        if modelo_esperado and modelo_perfil and modelo_perfil != modelo_esperado:
            raise ErrorCrealityPrint(
                f"El perfil de máquina «{maquina}» es de una «{modelo_perfil}» y la impresora es una "
                f"«{modelo_esperado}». Elige un perfil de «{modelo_esperado}»."
            )
        _comprobar_compatibles(planos, maquina, res.avisos)
        if proyecto:
            # Los colores del proyecto se conservan: el CFS los usa para casar ranuras.
            for i, p in enumerate(planos["filament"]):
                if i < len(proyecto["colores"]) and proyecto["colores"][i]:
                    p["filament_colour"] = [proyecto["colores"][i]]
        if ajustes:
            res.ajustes_aplicados = aplicar_ajustes(planos, ajustes)
        rutas = {}
        for tipo, lista in planos.items():
            rutas[tipo] = []
            for i, p in enumerate(lista):
                f = trabajo / f"{tipo}_{i}.json"
                f.write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")
                rutas[tipo].append(str(f))
        cmd += ["--load-settings", ";".join(rutas["machine"] + rutas["process"]),
                "--load-filaments", ";".join(rutas["filament"])]
        res.perfiles = {"origen": "forzados", "maquina": maquina, "proceso": proceso, "filamentos": filamentos}
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
        destino_log = salida / f"{_nombre_seguro(modelo.stem)}_error_laminado.log"
        if log.is_file():
            shutil.copyfile(log, destino_log)
        motivo = _motivo_error(log) or (f"se pasó de {tiempo_max} s" if codigo is None else f"código {codigo}")
        shutil.rmtree(trabajo, ignore_errors=True)
        raise ErrorCrealityPrint(f"Creality Print no ha laminado {modelo.name}: {motivo}. Log: {destino_log}")

    maquina_plana = cp.resolver_perfil("machine", res.perfiles["maquina"]) if res.perfiles.get("maquina") else {}
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
                res.avisos.append(f"Placa {n_placa}: no he podido generar la miniatura.")
        tipo = (inf.lista("filament_type") or ["PLA"])[0]
        nombre = f"{_nombre_seguro(modelo.name)}_{tipo}_{_nombre_tiempo(inf.tiempo_estimado)}"
        if len(generados) > 1:
            nombre += f"_placa{n_placa}"
        destino = salida / f"{nombre}.gcode"
        os.replace(g, destino)
        inf.ruta = str(destino)
        res.gcodes.append({"placa": n_placa, **inf.resumen()})
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
                avisos.append(f"El perfil «{p.get('name')}» no declara compatible «{maquina}».")


# Lo que Creality Print escribe en el log y lo que significa para quien lamina.
_EXPLICACIONES = [
    (r"gcode path conflicts found between (.+?) and (.+)",
     "las trayectorias de «{0}» y «{1}» chocan (a menudo la torre de purga no cabe: prueba "
     "ajustes={{'enable_prime_tower': 0}} o separa las piezas en Creality Print)"),
    (r"found slicing result conflict", "hay un conflicto de trayectorias entre objetos"),
    (r"outside of plate|out of (the )?plate|outside the print area", "el modelo se sale de la cama"),
    (r"File Version .* not supported", "el 3MF es de otra versión de Creality Print"),
    (r"got error when validate: (.+)", "ajustes incompatibles: {0}"),
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
        raise ErrorCrealityPrint(f"No existe {ruta}.")
    cmd = [str(cp.exe)]
    if en_ventana_abierta:
        cmd.append("--single-instance")
    cmd.append(str(ruta))
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(cmd, creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return "en la ventana abierta" if en_ventana_abierta else "en una ventana nueva"
