"""CREALITY.md: qué perfiles, ajustes y reglas usar, como un AGENTS.md para imprimir.

Dos niveles:

- **Global**: `CREALITY.md` junto a la configuración del plugin
  (%APPDATA%\\creality-print-plugin\\). Vale para todo.
- **De proyecto**: un `CREALITY.md` en la carpeta del modelo o en cualquiera de las de
  encima. Vale para lo que haya dentro.

Cada archivo tiene arriba, entre `---`, lo que el plugin aplica solo (YAML) y debajo
texto libre: reglas que Claude lee y sigue. Manda el más cercano al modelo:
global < proyecto más alto < proyecto más cercano < lo que se pida en la conversación.

    ---
    process: 0.16mm Standard @Creality K2 0.4 nozzle
    filaments: [Hyper PLA @Creality K2 0.4 nozzle]
    settings:
      brim_type: outer_only
      wall_loops: 3
    slots: {"1": 1D}
    output_dir: gcode
    ---
    # Wall brackets
    Everything in black PLA. Outdoor parts: 4 walls.

Las claves van en inglés; se aceptan también las primeras, en español (maquina,
proceso, filamentos, ajustes, carpeta_salida, ranuras) y la sección '## Notas'.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

import config

NOMBRE = "CREALITY.md"
CLAVES = {
    "machine": "Creality Print machine profile",
    "process": "process profile",
    "filaments": "filament profiles, one per extruder",
    "settings": "profile keys to change, e.g. {wall_loops: 3}",
    "output_dir": "where to put the gcode (relative to the CREALITY.md)",
    "slots": "extruder -> CFS slot to suggest when printing, e.g. {'1': 1D}",
}
# Las claves de la primera versión, en español.
ALIAS = {"maquina": "machine", "proceso": "process", "filamentos": "filaments", "ajustes": "settings",
         "carpeta_salida": "output_dir", "ranuras": "slots"}
DICCIONARIOS = ("settings", "slots")
_RE_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)
SECCION_NOTAS = "## Notes"
SECCIONES_NOTAS = ("## Notes", "## Notas")


class ErrorPreconfig(ValueError):
    pass


def ruta_global() -> Path:
    return config.ruta().parent / NOMBRE


# ---------------------------------------------------------------- leer y escribir

def leer(ruta: Path) -> tuple[dict, str]:
    texto = ruta.read_text(encoding="utf-8-sig")
    m = _RE_FRONT.match(texto)
    if not m:
        return {}, texto
    try:
        datos = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise ErrorPreconfig(f"{ruta}: the block between --- isn't valid YAML ({e}).") from e
    if not isinstance(datos, dict):
        raise ErrorPreconfig(f"{ruta}: the block between --- must be a list of key: value pairs.")
    return _normalizar(datos, ruta), texto[m.end():]


def _normalizar(datos: dict, ruta: Path) -> dict:
    datos = {ALIAS.get(k, k): v for k, v in datos.items()}
    desconocidas = set(datos) - set(CLAVES)
    if desconocidas:
        raise ErrorPreconfig(
            f"{ruta}: unknown keys {sorted(desconocidas)}. Valid: {', '.join(CLAVES)}. "
            "Slicing settings go inside `settings:`."
        )
    salida = {}
    for clave, valor in datos.items():
        if valor is None:
            continue
        if clave == "filaments":
            salida[clave] = [str(v) for v in (valor if isinstance(valor, list) else [valor])]
        elif clave in DICCIONARIOS:
            if not isinstance(valor, dict):
                raise ErrorPreconfig(f"{ruta}: `{clave}` must be a mapping.")
            salida[clave] = {str(k): v for k, v in valor.items()} if clave == "slots" else dict(valor)
        elif clave == "output_dir":
            salida[clave] = str((ruta.parent / str(valor)).resolve())
        else:
            salida[clave] = str(valor)
    return salida


def escribir(ruta: Path, datos: dict, cuerpo: str) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    texto = cuerpo.lstrip("\n")
    if datos:
        front = yaml.safe_dump(datos, allow_unicode=True, sort_keys=False, default_flow_style=False).strip()
        texto = f"---\n{front}\n---\n\n{texto}"
    tmp = ruta.with_suffix(".tmp")
    tmp.write_text(texto, encoding="utf-8")
    os.replace(tmp, ruta)


def _para_guardar(datos: dict, ruta: Path) -> dict:
    """Lo contrario de _normalizar: carpeta_salida vuelve a ser relativa si se puede."""
    salida = dict(datos)
    if "output_dir" in salida:
        try:
            salida["output_dir"] = os.path.relpath(salida["output_dir"], ruta.parent)
        except ValueError:  # otra unidad
            pass
    return salida


# ---------------------------------------------------------------- buscar y combinar

def archivos(desde: str | os.PathLike | None) -> list[Path]:
    """Los CREALITY.md que aplican, del menos al más importante."""
    encontrados: list[Path] = []
    g = ruta_global()
    if g.is_file():
        encontrados.append(g)
    if desde:
        d = Path(desde).resolve()
        if not d.is_dir():
            d = d.parent
        cadena = []
        for carpeta in [d, *d.parents]:
            f = carpeta / NOMBRE
            if f.is_file() and f.resolve() != g.resolve():
                cadena.append(f)
        encontrados += reversed(cadena)
    return encontrados


def resolver(desde: str | os.PathLike | None) -> dict:
    """Combina los CREALITY.md que aplican a `desde` (un modelo o una carpeta)."""
    valores: dict = {}
    reglas = []
    usados = []
    for f in archivos(desde):
        datos, cuerpo = leer(f)
        usados.append(str(f))
        for clave, valor in datos.items():
            if clave in DICCIONARIOS:
                valores[clave] = {**valores.get(clave, {}), **valor}
            else:
                valores[clave] = valor
        if cuerpo.strip():
            reglas.append({"archivo": str(f), "texto": cuerpo.strip()})
    return {"valores": valores, "reglas": reglas, "archivos": usados}


def actualizar(ruta: Path, cambios: dict, quitar: list[str] | None = None) -> dict:
    """Mezcla `cambios` en el bloque YAML de `ruta` sin tocar el texto de debajo."""
    datos, cuerpo = leer(ruta) if ruta.is_file() else ({}, "")
    nuevos = _normalizar(cambios, ruta)
    for clave, valor in nuevos.items():
        if clave in DICCIONARIOS:
            datos[clave] = {**datos.get(clave, {}), **valor}
        else:
            datos[clave] = valor
    for clave in quitar or []:
        partes = clave.split(".", 1)
        partes[0] = ALIAS.get(partes[0], partes[0])
        if len(partes) == 2 and partes[0] in DICCIONARIOS:
            datos.get(partes[0], {}).pop(partes[1], None)
            if not datos.get(partes[0]):
                datos.pop(partes[0], None)
        else:
            datos.pop(ALIAS.get(clave, clave), None)
    if not cuerpo.strip():
        cuerpo = "# Creality Print presets\n\nFree-text rules for Claude.\n"
    escribir(ruta, _para_guardar(datos, ruta), cuerpo)
    return datos


# ---------------------------------------------------------------- notas (en el global)

def _secciones(cuerpo: str) -> tuple[str, list[str], str]:
    """(antes, viñetas de ## Notas, después)."""
    lineas = cuerpo.splitlines()
    try:
        i = next(n for n, l in enumerate(lineas) if l.strip() in SECCIONES_NOTAS)
    except StopIteration:
        return cuerpo, [], ""
    fin = next((n for n in range(i + 1, len(lineas)) if lineas[n].startswith("#")), len(lineas))
    vinetas = [l.strip()[2:].strip() for l in lineas[i + 1:fin] if l.strip().startswith(("- ", "* "))]
    return "\n".join(lineas[:i]), vinetas, "\n".join(lineas[fin:])


def _guardar_notas(vinetas: list[str]) -> None:
    ruta = ruta_global()
    datos, cuerpo = leer(ruta) if ruta.is_file() else ({}, "")
    antes, _, despues = _secciones(cuerpo)
    encabezado = next((l.strip() for l in cuerpo.splitlines() if l.strip() in SECCIONES_NOTAS), SECCION_NOTAS)
    if not antes.strip():
        antes = "# My printing rules\n\nWhat always applies, wherever the model is."
    bloque = encabezado + "\n\n" + "\n".join(f"- {v}" for v in vinetas) + "\n"
    cuerpo = antes.rstrip() + "\n\n" + bloque + (("\n" + despues.strip() + "\n") if despues.strip() else "")
    escribir(ruta, _para_guardar(datos, ruta), cuerpo)


def notas() -> list[str]:
    _migrar_notas_json()
    ruta = ruta_global()
    if not ruta.is_file():
        return []
    return _secciones(leer(ruta)[1])[1]


def anadir_nota(texto: str) -> list[str]:
    texto = " ".join(texto.split())
    if not texto:
        raise ErrorPreconfig("The note is empty.")
    lista = notas()
    if not any(n.lower() == texto.lower() for n in lista):
        lista.append(texto)
        _guardar_notas(lista)
    return lista


def quitar_nota(numero: int) -> list[str]:
    lista = notas()
    if not 1 <= numero <= len(lista):
        raise ErrorPreconfig(f"There is no note {numero}; there are {len(lista)}.")
    lista.pop(numero - 1)
    _guardar_notas(lista)
    return lista


def _migrar_notas_json() -> None:
    """Las primeras versiones guardaban las notas en config.json: se pasan al CREALITY.md global."""
    datos = config.leer()
    viejas = datos.pop("notas", None)
    if not viejas:
        return
    ruta = ruta_global()
    actuales = _secciones(leer(ruta)[1])[1] if ruta.is_file() else []
    for n in viejas:
        texto = n.get("texto", "") if isinstance(n, dict) else str(n)
        if texto and texto not in actuales:
            actuales.append(texto)
    _guardar_notas(actuales)
    config.guardar(datos)
