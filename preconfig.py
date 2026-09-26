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
    proceso: 0.16mm Standard @Creality K2 0.4 nozzle
    filamentos: [Hyper PLA @Creality K2 0.4 nozzle]
    ajustes:
      brim_type: outer_only
      wall_loops: 3
    ranuras: {"1": 1D}
    carpeta_salida: gcode
    ---
    # Soportes de pared
    Todo en PLA negro. Piezas de exterior: 4 paredes.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

import config

NOMBRE = "CREALITY.md"
CLAVES = {
    "maquina": "perfil de máquina de Creality Print",
    "proceso": "perfil de proceso",
    "filamentos": "lista de perfiles de filamento, uno por extrusor",
    "ajustes": "claves del perfil a cambiar, p. ej. {wall_loops: 3}",
    "carpeta_salida": "dónde dejar los gcode (relativa al CREALITY.md)",
    "ranuras": "extrusor -> ranura del CFS que proponer al imprimir, p. ej. {'1': 1D}",
}
_RE_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)
SECCION_NOTAS = "## Notas"


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
        raise ErrorPreconfig(f"{ruta}: el bloque entre --- no es YAML válido ({e}).") from e
    if not isinstance(datos, dict):
        raise ErrorPreconfig(f"{ruta}: el bloque entre --- debe ser una lista de claves: valor.")
    return _normalizar(datos, ruta), texto[m.end():]


def _normalizar(datos: dict, ruta: Path) -> dict:
    desconocidas = set(datos) - set(CLAVES)
    if desconocidas:
        raise ErrorPreconfig(
            f"{ruta}: claves desconocidas {sorted(desconocidas)}. Valen: {', '.join(CLAVES)}. "
            "Los ajustes de laminado van dentro de `ajustes:`."
        )
    salida = {}
    for clave, valor in datos.items():
        if valor is None:
            continue
        if clave == "filamentos":
            salida[clave] = [str(v) for v in (valor if isinstance(valor, list) else [valor])]
        elif clave in ("ajustes", "ranuras"):
            if not isinstance(valor, dict):
                raise ErrorPreconfig(f"{ruta}: `{clave}` debe ser un diccionario.")
            salida[clave] = {str(k): v for k, v in valor.items()} if clave == "ranuras" else dict(valor)
        elif clave == "carpeta_salida":
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
    if "carpeta_salida" in salida:
        try:
            salida["carpeta_salida"] = os.path.relpath(salida["carpeta_salida"], ruta.parent)
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
            if clave in ("ajustes", "ranuras"):
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
        if clave in ("ajustes", "ranuras"):
            datos[clave] = {**datos.get(clave, {}), **valor}
        else:
            datos[clave] = valor
    for clave in quitar or []:
        partes = clave.split(".", 1)
        if len(partes) == 2 and partes[0] in ("ajustes", "ranuras"):
            datos.get(partes[0], {}).pop(partes[1], None)
            if not datos.get(partes[0]):
                datos.pop(partes[0], None)
        else:
            datos.pop(clave, None)
    if not cuerpo.strip():
        cuerpo = "# Preconfiguración de Creality Print\n\nReglas en texto libre para Claude.\n"
    escribir(ruta, _para_guardar(datos, ruta), cuerpo)
    return datos


# ---------------------------------------------------------------- notas (en el global)

def _secciones(cuerpo: str) -> tuple[str, list[str], str]:
    """(antes, viñetas de ## Notas, después)."""
    lineas = cuerpo.splitlines()
    try:
        i = next(n for n, l in enumerate(lineas) if l.strip() == SECCION_NOTAS)
    except StopIteration:
        return cuerpo, [], ""
    fin = next((n for n in range(i + 1, len(lineas)) if lineas[n].startswith("#")), len(lineas))
    vinetas = [l.strip()[2:].strip() for l in lineas[i + 1:fin] if l.strip().startswith(("- ", "* "))]
    return "\n".join(lineas[:i]), vinetas, "\n".join(lineas[fin:])


def _guardar_notas(vinetas: list[str]) -> None:
    ruta = ruta_global()
    datos, cuerpo = leer(ruta) if ruta.is_file() else ({}, "")
    antes, _, despues = _secciones(cuerpo)
    if not antes.strip():
        antes = "# Mis reglas de impresión\n\nLo que vale siempre, esté donde esté el modelo."
    bloque = SECCION_NOTAS + "\n\n" + "\n".join(f"- {v}" for v in vinetas) + "\n"
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
        raise ErrorPreconfig("La nota está vacía.")
    lista = notas()
    if not any(n.lower() == texto.lower() for n in lista):
        lista.append(texto)
        _guardar_notas(lista)
    return lista


def quitar_nota(numero: int) -> list[str]:
    lista = notas()
    if not 1 <= numero <= len(lista):
        raise ErrorPreconfig(f"No hay nota {numero}; hay {len(lista)}.")
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
