# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "mcp>=1.27,<2",
#     "httpx>=0.27",
#     "websockets>=13",
#     "pillow>=10",
# ]
# ///
"""Servidor MCP: Creality Print + Creality K2 para Claude Code.

Lamina con la CLI de Creality Print (no con Orca), revisa cada gcode antes de que
llegue a la impresora y la maneja igual que Creality Print: Moonraker para leer y
subir, websocket 9999 para lanzar con el CFS.

Todo lo que mueve la impresora (imprimir, cancelar) exige una confirmación explícita
con el nombre del archivo, y la skill manda preguntar antes a la persona.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import logging

import anyio
from mcp.server.fastmcp import FastMCP

import config
import creality_print as cpm
import gcode
import k2 as k2m

logging.getLogger("httpx").setLevel(logging.WARNING)

mcp = FastMCP("creality-print")

_cp: cpm.CrealityPrint | None = None
_k2: k2m.K2 | None = None


def cp() -> cpm.CrealityPrint:
    global _cp
    if _cp is None:
        _cp = cpm.CrealityPrint.detectar()
    return _cp


def impresora() -> k2m.K2:
    global _k2
    if _k2 is None:
        _k2 = k2m.K2.detectar()
    return _k2


def maquina_destino() -> tuple[str, tuple[int, int, int]] | None:
    """Modelo y cama de la impresora de verdad, aunque ahora no esté encendida.

    Sale de la impresora si responde y, si no, de la que tiene guardada Creality Print.
    """
    try:
        m = impresora().modelo
        if m:
            return m
    except k2m.ErrorImpresora:
        pass
    guardada = config.impresora_guardada()
    if guardada and guardada[1] in k2m.MODELOS:
        return k2m.MODELOS[guardada[1]]
    for _, codigo in k2m._hosts_de_creality_print():
        if codigo in k2m.MODELOS:
            return k2m.MODELOS[codigo]
    return None


def _analizar(ruta: Path) -> gcode.Informe:
    destino = maquina_destino()
    if destino:
        return gcode.analizar(ruta, destino[0], destino[1])
    inf = gcode.analizar(ruta)
    inf.avisos.append("No sé qué impresora es: no he podido comprobar modelo ni límites contra la máquina real.")
    return inf


def _error(e: Exception) -> dict:
    return {"error": str(e)}


# ---------------------------------------------------------------- configuración personal

@mcp.tool()
def configurar_impresora(host: str | None = None) -> dict:
    """Busca la impresora y la guarda en la configuración del plugin (fuera del repo).

    Sin `host`: prueba las impresoras que tiene Creality Print y, si ninguna responde,
    escanea la red local. Con `host` (IP o nombre): comprueba ese y lo guarda. Si aparece
    más de una, devuelve la lista para que la persona elija y se vuelve a llamar con su host.
    Úsalo la primera vez o cuando la impresora "desaparezca" (cambio de IP por DHCP).
    """
    global _k2
    if host:
        encontradas = [r for r in [config.identificar(host)] if r]
        if not encontradas:
            return {"error": f"En {host} no hay una impresora Creality con Moonraker (puerto 7125)."}
    else:
        encontradas = config.buscar_impresoras()
        if not encontradas:
            return {"error": "No he encontrado ninguna impresora Creality en la red. ¿Está encendida y en la "
                             "misma red que este PC? Pásame su IP (sale en la pantalla, Ajustes → Red)."}
    soportadas = [r for r in encontradas if r["soportada"]]
    if len(soportadas) != 1:
        return {"elegir": encontradas,
                "motivo": "Hay varias impresoras: pregunta cuál y vuelve a llamar con su host."
                if len(soportadas) > 1 else "Ninguna es un modelo soportado (K2, K2 Pro, K2 Plus)."}
    elegida = soportadas[0]
    config.guardar_impresora(elegida["host"], elegida["modelo_codigo"], elegida["nombre"])
    _k2 = None
    return {"guardada": elegida, "configuracion": str(config.ruta())}


@mcp.tool()
def mis_notas() -> dict:
    """Las reglas y preferencias que la persona ha pedido recordar (temperaturas de sus filamentos,
    que siempre quiere brim, qué ranura usa para qué…). Léelas antes de laminar o imprimir y
    aplícalas. También dice qué impresora hay guardada."""
    datos = config.leer()
    return {
        "impresora": datos.get("impresora"),
        "notas": [{"numero": i + 1, **n} for i, n in enumerate(datos.get("notas", []))],
        "configuracion": str(config.ruta()),
    }


@mcp.tool()
def guardar_nota(texto: str) -> dict:
    """Guarda una regla o preferencia duradera de la persona, p. ej. "Mi ABS High Speed va a 270 °C"
    o "En piezas de más de 100 mm, brim de 5 mm". Solo cuando lo pida o lo confirme."""
    try:
        return {"notas": [{"numero": i + 1, **n} for i, n in enumerate(config.anadir_nota(texto))]}
    except ValueError as e:
        return _error(e)


@mcp.tool()
def borrar_nota(numero: int) -> dict:
    """Borra la nota con ese número (el que da mis_notas)."""
    try:
        return {"notas": [{"numero": i + 1, **n} for i, n in enumerate(config.quitar_nota(numero))]}
    except ValueError as e:
        return _error(e)


# ---------------------------------------------------------------- Creality Print

@mcp.tool()
def creality_print_info() -> dict:
    """Instalación de Creality Print: versión, carpetas, perfiles seleccionados ahora en la ventana
    y la impresora de destino (modelo y cama). Úsalo primero si algo falla."""
    try:
        info = cp().info()
    except cpm.ErrorCrealityPrint as e:
        return _error(e)
    destino = maquina_destino()
    info["impresora_destino"] = {"modelo": destino[0], "cama_mm": destino[1]} if destino else None
    info["notas_guardadas"] = len(config.notas())
    return info


@mcp.tool()
def listar_perfiles(tipo: str, filtro: str = "", maquina: str | None = None) -> dict:
    """Perfiles de Creality Print que se pueden elegir.

    tipo: "machine", "process" o "filament". filtro: palabras que deben aparecer todas,
    p. ej. "PLA" o "0.16mm". Los procesos y filamentos se limitan a los de `maquina`
    (por defecto la seleccionada en Creality Print, p. ej. "Creality K2 0.4 nozzle");
    maquina="" los enseña todos. Los de origen "usuario" son los que ha creado él.
    """
    try:
        perfiles = cp().listar_perfiles(tipo, filtro, maquina)
    except cpm.ErrorCrealityPrint as e:
        return _error(e)
    return {"total": len(perfiles), "perfiles": perfiles[:200]}


@mcp.tool()
def ver_ajustes_perfil(tipo: str, nombre: str, claves: list[str] | None = None) -> dict:
    """Valores de un perfil ya resuelto (con toda su herencia). Sin `claves` devuelve
    solo los nombres de las claves; con `claves` sus valores. Sirve para saber cómo se
    llama un ajuste antes de cambiarlo en `laminar`."""
    try:
        plano = cp().resolver_perfil(tipo, nombre)
    except cpm.ErrorCrealityPrint as e:
        return _error(e)
    if not claves:
        return {"perfil": nombre, "claves": sorted(plano)}
    return {"perfil": nombre, "valores": {c: plano.get(c, "(no existe)") for c in claves}}


@mcp.tool()
async def laminar(
    modelo: str,
    maquina: str | None = None,
    proceso: str | None = None,
    filamentos: list[str] | None = None,
    ajustes: dict | None = None,
    carpeta_salida: str | None = None,
    usar_perfiles_del_3mf: bool = False,
    colocar: bool = False,
    orientar: bool = False,
) -> dict:
    """Lamina un STL, 3MF, OBJ o STEP con Creality Print (su propio motor, no Orca) y revisa el gcode.

    - Sin perfiles usa los que están seleccionados ahora en la ventana de Creality Print.
    - filamentos: uno por extrusor del modelo (T0, T1…). En un 3MF multicolor, en el
      orden de sus filamentos.
    - ajustes: claves del perfil a cambiar, p. ej. {"sparse_infill_density": "20%",
      "wall_loops": 3, "brim_type": "outer_only", "enable_support": 1}. Una clave que no
      existe da error (la CLI la ignoraría en silencio).
    - Un 3MF descargado trae la máquina de quien lo hizo (K1C, K2 Pro…): por defecto se
      imponen los perfiles de la K2. usar_perfiles_del_3mf solo si se sabe que son buenos.
    - colocar/orientar: auto-colocar y auto-orientar como el botón de la ventana.

    Devuelve cada gcode con su revisión: `apto` false significa que NO debe imprimirse.
    El gcode se guarda junto al modelo (o en carpeta_salida) con el nombre que usaría
    Creality Print, y lleva miniaturas para la pantalla de la impresora.
    """
    destino = maquina_destino()

    def trabajo():
        return cpm.laminar(
            cp(), modelo, maquina=maquina, proceso=proceso, filamentos=filamentos, salida=carpeta_salida,
            ajustes=ajustes, usar_perfiles_del_3mf=usar_perfiles_del_3mf, colocar=colocar, orientar=orientar,
            modelo_esperado=destino[0] if destino else None, cama=destino[1] if destino else None,
        )

    try:
        res = await anyio.to_thread.run_sync(trabajo)
    except cpm.ErrorCrealityPrint as e:
        return _error(e)
    return {
        "gcodes": res.gcodes,
        "perfiles": res.perfiles,
        "ajustes_aplicados": res.ajustes_aplicados,
        "avisos": res.avisos,
        "segundos": res.segundos,
        "impresora_destino": destino[0] if destino else None,
    }


@mcp.tool()
async def analizar_gcode(archivo: str) -> dict:
    """Revisa un gcode: para qué impresora es, perfiles, filamentos/extrusores que usa, tiempo,
    miniaturas y si el cabezal se sale de la cama REAL de la impresora.

    archivo: ruta local o nombre de un archivo que ya está en la impresora. Detecta el fallo
    de Creality Print que lamina con el perfil K2 Pro (cama de 300) sin avisar.
    """
    ruta = Path(archivo)

    def trabajo():
        if ruta.is_file():
            return _analizar(ruta).resumen()
        with tempfile.TemporaryDirectory() as tmp:
            local = impresora().descargar(archivo, Path(tmp) / "g.gcode")
            r = _analizar(local).resumen()
            r["archivo"] = f"(impresora) {archivo}"
            return r

    try:
        return await anyio.to_thread.run_sync(trabajo)
    except (k2m.ErrorImpresora, OSError) as e:
        return _error(e)


@mcp.tool()
def abrir_en_creality_print(ruta: str, en_ventana_abierta: bool = False) -> dict:
    """Abre un modelo, 3MF o gcode en Creality Print para que la persona lo revise a ojo.

    Por defecto en una ventana nueva. en_ventana_abierta=True lo mete en la ventana que ya
    tiene abierta, lo que AÑADE el modelo a su proyecto actual: pregúntale antes."""
    try:
        donde = cpm.abrir(cp(), ruta, en_ventana_abierta)
    except cpm.ErrorCrealityPrint as e:
        return _error(e)
    return {"abierto": ruta, "donde": donde}


# ---------------------------------------------------------------- impresora

@mcp.tool()
def estado_impresora() -> dict:
    """Estado de la K2: libre/imprimiendo, archivo, progreso, capa, tiempo restante, temperaturas
    y si la malla de cama está cargada. Solo lectura."""
    try:
        return impresora().estado()
    except k2m.ErrorImpresora as e:
        return _error(e)


@mcp.tool()
def estado_cfs() -> dict:
    """Qué hay en cada ranura del CFS (1A–1D…): material, nombre, color, si está vacía y cuál
    está en uso, más el mapeo extrusor→ranura del último trabajo. Solo lectura."""
    try:
        c = impresora().cfs()
    except k2m.ErrorImpresora as e:
        return _error(e)
    for r in c["ranuras"]:
        r.pop("_color_crudo", None)
    return c


@mcp.tool()
def archivos_impresora(filtro: str = "", limite: int = 20) -> dict:
    """Gcodes guardados en la impresora, los más recientes primero. Solo lectura."""
    try:
        return {"archivos": impresora().archivos(filtro, limite)}
    except k2m.ErrorImpresora as e:
        return _error(e)


@mcp.tool()
def historial_impresora(limite: int = 10) -> dict:
    """Últimos trabajos de la impresora con su resultado. Ojo: en la K2 un trabajo que acabó bien
    puede figurar como `cancelled` si el eje Y se protege al final. Solo lectura."""
    try:
        return {"trabajos": impresora().historial(limite)}
    except k2m.ErrorImpresora as e:
        return _error(e)


@mcp.tool()
async def subir_gcode(ruta: str, nombre: str | None = None) -> dict:
    """Sube un gcode local a la impresora SIN imprimirlo. Antes lo revisa y se niega si no es apto
    (otra impresora, se sale de la cama…)."""
    local = Path(ruta)
    if not local.is_file():
        return {"error": f"No existe {ruta}."}

    def trabajo():
        inf = _analizar(local)
        if not inf.apto:
            return {"error": "No lo subo: el gcode no es apto.", "revision": inf.resumen()}
        r = impresora().subir(local, nombre)
        r["revision"] = {"avisos": inf.avisos, "tiempo_estimado": inf.tiempo_estimado,
                         "extrusores": [t + 1 for t in inf.herramientas]}
        return r

    try:
        return await anyio.to_thread.run_sync(trabajo)
    except k2m.ErrorImpresora as e:
        return _error(e)


@mcp.tool()
async def imprimir(
    nombre: str,
    confirmacion: str,
    ranuras: dict[str, str] | None = None,
    bobina_externa: bool = False,
    calibracion: bool = False,
) -> dict:
    """LANZA una impresión de un gcode que ya está en la impresora. Mueve la máquina.

    Solo después de que la persona lo haya aprobado en esta conversación, con el archivo y
    las ranuras a la vista. `confirmacion` debe ser exactamente `nombre`.

    - ranuras: extrusor del gcode -> ranura del CFS, p. ej. {"1": "1D"} o {"1": "1A", "2": "1D"}.
      Obligatorio con el CFS (mira estado_cfs y los extrusores que da analizar_gcode).
      Es el mismo mapeo que hace el diálogo de colores de Creality Print.
    - bobina_externa: imprimir desde el portabobinas trasero, sin CFS.
    - calibracion: autocalibración antes de imprimir (tarda más).

    Antes de lanzar descarga el gcode de la impresora y lo revisa entero; si no es apto,
    si la impresora está ocupada o si el material de una ranura no coincide, no lanza.
    """
    if confirmacion != nombre:
        return {"error": "Falta la confirmación: pasa en `confirmacion` exactamente el nombre del archivo, "
                         "y solo después de que la persona lo haya aprobado."}
    if ranuras and bobina_externa:
        return {"error": "O ranuras del CFS o bobina externa, no las dos."}

    def trabajo():
        k = impresora()
        est = k.estado()
        if est["klipper"] != "ready":
            return {"error": f"Klipper no está listo ({est['klipper']})."}
        if est["estado"] not in k2m.LIBRE:
            return {"error": f"La impresora está ocupada ({est['estado']}: {est['archivo']})."}
        ws = k.estado_ws()
        if ws.get("deviceState") not in (0, None):
            return {"error": f"La pantalla de la impresora dice que está ocupada (deviceState {ws.get('deviceState')})."}
        with tempfile.TemporaryDirectory() as tmp:
            inf = _analizar(k.descargar(nombre, Path(tmp) / "g.gcode"))
        if not inf.apto:
            return {"error": "No lanzo: el gcode no es apto.", "revision": inf.resumen()}
        extrusores = [t + 1 for t in inf.herramientas]
        tipos = inf.lista("filament_type")
        mapa: dict[int, str] | None = None
        if not bobina_externa:
            if not ranuras:
                cfs = k.cfs()
                return {"error": "Faltan las ranuras: di qué ranura del CFS usa cada extrusor.",
                        "extrusores_del_gcode": [{"extrusor": e, "tipo": _en(tipos, e - 1),
                                                  "color": _en(inf.lista("filament_colour"), e - 1)}
                                                 for e in extrusores],
                        "cfs": [{k_: v for k_, v in r.items() if k_ != "_color_crudo"} for r in cfs["ranuras"]]}
            try:
                mapa = {int(e): r for e, r in ranuras.items()}
            except ValueError:
                return {"error": "Las claves de `ranuras` son números de extrusor: {\"1\": \"1D\"}."}
            faltan = [e for e in extrusores if e not in mapa]
            if faltan:
                return {"error": f"El gcode usa los extrusores {extrusores}; falta ranura para {faltan}."}
            cfs = {r["ranura"]: r for r in k.cfs()["ranuras"]}
            for e, r in mapa.items():
                caja, mat = k2m.ranura_a_indices(r)
                ranura = cfs.get(f"{caja}{'ABCD'[mat]}")
                if ranura is None or ranura["vacia"]:
                    return {"error": f"La ranura {r} está vacía o no existe."}
                pedido = _en(tipos, e - 1)
                if pedido and ranura["material"] and pedido.upper() != ranura["material"].upper():
                    return {"error": f"El extrusor {e} es {pedido} y la ranura {r} tiene {ranura['material']}. "
                                     "Relamina con el filamento correcto o cambia de ranura."}
        res = k.imprimir(nombre, mapa, tipos, calibracion)
        res["revision"] = {"avisos": inf.avisos, "tiempo_estimado": inf.tiempo_estimado}
        return res

    try:
        return await anyio.to_thread.run_sync(trabajo)
    except k2m.ErrorImpresora as e:
        return _error(e)


@mcp.tool()
def controlar_impresion(accion: str, confirmacion: str = "") -> dict:
    """Pausa, reanuda o cancela la impresión en curso (accion: "pausar", "reanudar", "cancelar").
    Cancelar no tiene vuelta atrás: exige confirmacion="cancelar" y el visto bueno de la persona."""
    if accion == "cancelar" and confirmacion != "cancelar":
        return {"error": "Para cancelar pasa confirmacion=\"cancelar\" después de que la persona lo apruebe."}
    try:
        k = impresora()
        est = k.estado()
        if est["estado"] not in ("printing", "paused"):
            return {"error": f"No hay nada imprimiéndose ({est['estado']})."}
        return k.control(accion)
    except k2m.ErrorImpresora as e:
        return _error(e)


def _en(lista: list, i: int):
    return lista[i] if 0 <= i < len(lista) else None


if __name__ == "__main__":
    mcp.run()
