# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "mcp>=1.27,<2",
#     "httpx>=0.27",
#     "websockets>=13",
#     "pillow>=10",
#     "pyyaml>=6",
# ]
# ///
"""Servidor MCP: Creality Print + impresoras Creality (K2, K1…) para Claude Code.

Lamina con la CLI de Creality Print (no con Orca), revisa cada gcode antes de que
llegue a la impresora y la maneja igual que Creality Print: Moonraker para leer y
subir, websocket 9999 para lanzar con el CFS.

Todo lo que mueve la impresora (imprimir, cancelar) exige una confirmación explícita
con el nombre del archivo, y la skill manda preguntar antes a la persona.
"""
from __future__ import annotations

import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import logging

import anyio
from mcp.server.fastmcp import FastMCP

import config
import creality_print as cpm
import gcode
import impresora as imp
import preconfig

logging.getLogger("httpx").setLevel(logging.WARNING)

mcp = FastMCP("creality-print")

_cp: cpm.CrealityPrint | None = None
_k2: imp.Impresora | None = None


def cp() -> cpm.CrealityPrint:
    global _cp
    if _cp is None:
        _cp = cpm.CrealityPrint.detectar()
    return _cp


def impresora() -> imp.Impresora:
    global _k2
    if _k2 is None:
        _k2 = imp.Impresora.detectar()
    return _k2


def maquina_destino() -> tuple[str, tuple[int, int, int]] | None:
    """Modelo y cama de la impresora de verdad, aunque ahora no esté encendida.

    Sale de la impresora si responde y, si no, de la que tiene guardada Creality Print.
    """
    try:
        m = impresora().modelo
        if m:
            return m
    except imp.ErrorImpresora:
        pass
    guardada = config.impresora_guardada()
    if guardada and guardada[1] in imp.MODELOS:
        return imp.MODELOS[guardada[1]]
    for _, codigo in imp._hosts_de_creality_print():
        if codigo in imp.MODELOS:
            return imp.MODELOS[codigo]
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
                if len(soportadas) > 1 else "Ninguna es un modelo que conozca Creality Print."}
    elegida = soportadas[0]
    config.guardar_impresora(elegida["host"], elegida["modelo_codigo"], elegida["nombre"])
    _k2 = None
    return {"guardada": elegida, "configuracion": str(config.ruta())}


@mcp.tool()
def ver_preconfiguracion(modelo_o_carpeta: str | None = None) -> dict:
    """Lo que hay que usar según los CREALITY.md (como un AGENTS.md para imprimir).

    Combina el global (vale siempre) y los del proyecto: el de la carpeta del modelo y los
    de las carpetas de encima; manda el más cercano. `valores` es lo que `laminar` aplica
    solo (perfiles, ajustes, carpeta de salida, ranuras a proponer); `reglas` es texto
    libre de la persona que hay que leer y seguir, incluidas sus notas. Llámalo antes de
    laminar o imprimir, con la ruta del modelo si la hay.
    """
    try:
        r = preconfig.resolver(modelo_o_carpeta)
    except preconfig.ErrorPreconfig as e:
        return _error(e)
    r["global"] = str(preconfig.ruta_global())
    if not r["archivos"]:
        r["pista"] = ("No hay ningún CREALITY.md. Se crean con guardar_preconfiguracion "
                      "(ambito 'global' o 'proyecto') o a mano; ver el README.")
    return r


@mcp.tool()
def guardar_preconfiguracion(
    ambito: str,
    carpeta: str | None = None,
    maquina: str | None = None,
    proceso: str | None = None,
    filamentos: list[str] | None = None,
    ajustes: dict | None = None,
    carpeta_salida: str | None = None,
    ranuras: dict[str, str] | None = None,
    quitar: list[str] | None = None,
) -> dict:
    """Crea o cambia un CREALITY.md con los perfiles y ajustes a usar. Solo si la persona lo pide.

    ambito: "global" (vale siempre) o "proyecto" (vale para `carpeta` y lo que haya dentro;
    `carpeta` obligatoria). Solo cambia lo que se pasa; el texto libre del archivo no se toca.
    quitar: claves a borrar, p. ej. ["proceso", "ajustes.brim_type"]. Comprueba que los
    perfiles existen y que los ajustes son claves reales antes de guardar.
    """
    if ambito == "global":
        ruta = preconfig.ruta_global()
    elif ambito == "proyecto":
        if not carpeta or not Path(carpeta).is_dir():
            return {"error": "Para ambito 'proyecto' hace falta `carpeta`, una carpeta que exista."}
        ruta = Path(carpeta).resolve() / preconfig.NOMBRE
    else:
        return {"error": "ambito debe ser 'global' o 'proyecto'."}
    cambios = {k: v for k, v in {"maquina": maquina, "proceso": proceso, "filamentos": filamentos,
                                  "ajustes": ajustes, "carpeta_salida": carpeta_salida,
                                  "ranuras": ranuras}.items() if v is not None}
    try:
        _validar_perfiles(cambios, ruta)
        datos = preconfig.actualizar(ruta, cambios, quitar)
    except (preconfig.ErrorPreconfig, cpm.ErrorCrealityPrint, imp.ErrorImpresora) as e:
        return _error(e)
    return {"archivo": str(ruta), "valores": datos}


def _validar_perfiles(cambios: dict, ruta: Path) -> None:
    """Perfiles que existen, de la impresora real, y ajustes que son claves de verdad."""
    efectivos = {**preconfig.resolver(ruta.parent)["valores"], **cambios}
    activos = cp().perfiles_activos
    maquina = efectivos.get("maquina") or activos["machine"]
    for tipo, nombre in (("machine", cambios.get("maquina")), ("process", cambios.get("proceso"))):
        if nombre:
            cp().resolver_perfil(tipo, nombre)
    for f in cambios.get("filamentos") or []:
        cp().resolver_perfil("filament", f)
    if cambios.get("maquina"):
        destino = maquina_destino()
        modelo = cp().resolver_perfil("machine", maquina).get("printer_model")
        if destino and modelo and modelo != destino[0]:
            raise cpm.ErrorCrealityPrint(f"«{maquina}» es de una «{modelo}» y la impresora es una «{destino[0]}».")
    for r in (cambios.get("ranuras") or {}).values():
        imp.ranura_a_indices(str(r))
    if cambios.get("ajustes"):
        planos = {
            "machine": [cp().resolver_perfil("machine", maquina)],
            "process": [cp().resolver_perfil("process", efectivos.get("proceso") or activos["process"])],
            "filament": [cp().resolver_perfil("filament", f)
                         for f in (efectivos.get("filamentos") or activos["filaments"][:1])],
        }
        cpm.aplicar_ajustes(planos, cambios["ajustes"])


@mcp.tool()
def mis_notas() -> dict:
    """Las notas de la persona (reglas que vale siempre recordar) y la impresora guardada.
    Viven en la sección «## Notas» del CREALITY.md global. ver_preconfiguracion ya las incluye."""
    return {
        "impresora": config.leer().get("impresora"),
        "notas": [{"numero": i + 1, "texto": n} for i, n in enumerate(preconfig.notas())],
        "archivo": str(preconfig.ruta_global()),
    }


@mcp.tool()
def guardar_nota(texto: str) -> dict:
    """Guarda una regla o preferencia duradera, p. ej. "Mi ABS High Speed va a 270 °C" o
    "En piezas de más de 100 mm, brim de 5 mm". Va al CREALITY.md global. Solo si lo pide o
    lo confirma."""
    try:
        return {"notas": [{"numero": i + 1, "texto": n} for i, n in enumerate(preconfig.anadir_nota(texto))]}
    except preconfig.ErrorPreconfig as e:
        return _error(e)


@mcp.tool()
def borrar_nota(numero: int) -> dict:
    """Borra la nota con ese número (el que da mis_notas)."""
    try:
        return {"notas": [{"numero": i + 1, "texto": n} for i, n in enumerate(preconfig.quitar_nota(numero))]}
    except preconfig.ErrorPreconfig as e:
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
    info["notas_guardadas"] = len(preconfig.notas())
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
    mezclas: list[dict] | None = None,
) -> dict:
    """Lamina un STL, 3MF, OBJ o STEP con Creality Print (su propio motor, no Orca) y revisa el gcode.

    - Sin perfiles usa los del CREALITY.md que aplique al modelo (ver_preconfiguracion) y, si
      no hay, los que están seleccionados ahora en la ventana de Creality Print. Lo que se pase
      aquí manda sobre el CREALITY.md; los `ajustes` se suman a los suyos.
    - filamentos: uno por extrusor del modelo (T0, T1…). En un 3MF multicolor, en el
      orden de sus filamentos.
    - ajustes: claves del perfil a cambiar, p. ej. {"sparse_infill_density": "20%",
      "wall_loops": 3, "brim_type": "outer_only", "enable_support": 1}. Una clave que no
      existe da error (la CLI la ignoraría en silencio).
    - Un 3MF descargado trae la máquina de quien lo hizo (K1C, K2 Pro…): por defecto se
      imponen tus perfiles. usar_perfiles_del_3mf solo si se sabe que son buenos.
    - mezclas: filamentos virtuales que alternan dos físicos, p. ej.
      [{"a": 1, "b": 2, "porcentaje_b": 50, "modo": "capas"}] (modos: "capas", "puntos",
      "simple"). Con N filamentos, la mezcla 1 es el extrusor N+1: asígnala a un objeto con
      preparar_multicolor. Cada cambio purga filamento: tarda y gasta bastante más.
    - colocar/orientar: auto-colocar y auto-orientar como el botón de la ventana.

    Devuelve cada gcode con su revisión: `apto` false significa que NO debe imprimirse.
    El gcode se guarda junto al modelo (o en carpeta_salida) con el nombre que usaría
    Creality Print, y lleva miniaturas para la pantalla de la impresora.
    """
    destino = maquina_destino()
    try:
        pre = preconfig.resolver(modelo)
    except preconfig.ErrorPreconfig as e:
        return _error(e)
    v = pre["valores"]
    if usar_perfiles_del_3mf:
        v = {k: x for k, x in v.items() if k in ("carpeta_salida", "ranuras")}
    maquina = maquina or v.get("maquina")
    proceso = proceso or v.get("proceso")
    filamentos = filamentos or v.get("filamentos")
    carpeta_salida = carpeta_salida or v.get("carpeta_salida")
    ajustes_finales = {**v.get("ajustes", {}), **(ajustes or {})} or None

    def trabajo():
        return cpm.laminar(
            cp(), modelo, maquina=maquina, proceso=proceso, filamentos=filamentos, salida=carpeta_salida,
            ajustes=ajustes_finales, usar_perfiles_del_3mf=usar_perfiles_del_3mf, colocar=colocar,
            orientar=orientar, modelo_esperado=destino[0] if destino else None,
            cama=destino[1] if destino else None, mezclas=mezclas,
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
        "preconfiguracion": {"archivos": pre["archivos"], "ranuras_propuestas": v.get("ranuras"),
                             "reglas": pre["reglas"]} if pre["archivos"] else None,
    }


@mcp.tool()
def ver_objetos_3mf(modelo: str) -> dict:
    """Los objetos de un 3MF (nombre, id y extrusor asignado). Sirve para saber a qué
    objeto dar cada color con preparar_multicolor."""
    try:
        return {"objetos": cpm.objetos_3mf(Path(modelo))}
    except (OSError, KeyError, zipfile.BadZipFile) as e:
        return _error(e)


@mcp.tool()
def preparar_multicolor(
    modelo: str,
    asignar: dict[str, int] | None = None,
    cambios_altura: list[dict] | None = None,
    salida: str | None = None,
) -> dict:
    """Prepara un 3MF multicolor a partir de un STL o un 3MF, sin tocar el original.

    - asignar: {nombre o id del objeto: extrusor}, p. ej. {"Texto": 2}. Extrusor 1 = primer
      filamento (T0), 2 = segundo… Si luego se laminan `mezclas`, las mezclas son los
      números siguientes (con 2 filamentos, la mezcla 1 es el 3).
    - cambios_altura: [{"z": 10, "extrusor": 2}] cambia de filamento a esa altura: base de
      un color y relieve de otro, como en los carteles y las litofanías de color.

    No pinta caras sueltas: para eso, Creality Print a mano. Devuelve el 3MF nuevo; después,
    `laminar` con un filamento por extrusor (y `mezclas` si hay extrusores virtuales).
    """
    destino = maquina_destino()
    try:
        return cpm.preparar_multicolor(modelo, asignar, cambios_altura, salida,
                                       destino[1][:2] if destino else (260, 260))
    except (cpm.ErrorCrealityPrint, OSError, KeyError, ValueError, zipfile.BadZipFile) as e:
        return _error(e)


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
    except (imp.ErrorImpresora, OSError) as e:
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
    """Estado de la impresora: libre/imprimiendo, archivo, progreso, capa, tiempo restante, temperaturas
    y si la malla de cama está cargada. Solo lectura."""
    try:
        return impresora().estado()
    except imp.ErrorImpresora as e:
        return _error(e)


@mcp.tool()
def estado_cfs() -> dict:
    """Qué hay en cada ranura del CFS (1A–1D…): material, nombre, color, si está vacía y cuál
    está en uso, más el mapeo extrusor→ranura del último trabajo. Solo lectura."""
    try:
        c = impresora().cfs()
    except imp.ErrorImpresora as e:
        return _error(e)
    for r in c["ranuras"]:
        r.pop("_color_crudo", None)
    return c


@mcp.tool()
def archivos_impresora(filtro: str = "", limite: int = 20) -> dict:
    """Gcodes guardados en la impresora, los más recientes primero. Solo lectura."""
    try:
        return {"archivos": impresora().archivos(filtro, limite)}
    except imp.ErrorImpresora as e:
        return _error(e)


@mcp.tool()
def historial_impresora(limite: int = 10) -> dict:
    """Últimos trabajos de la impresora con su resultado. Ojo: en la K2 un trabajo que acabó bien
    puede figurar como `cancelled` si el eje Y se protege al final. Solo lectura."""
    try:
        return {"trabajos": impresora().historial(limite)}
    except imp.ErrorImpresora as e:
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
    except imp.ErrorImpresora as e:
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
        if est["estado"] not in imp.LIBRE:
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
                caja, mat = imp.ranura_a_indices(r)
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
    except imp.ErrorImpresora as e:
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
    except imp.ErrorImpresora as e:
        return _error(e)


def _en(lista: list, i: int):
    return lista[i] if 0 <= i < len(lista) else None


if __name__ == "__main__":
    mcp.run()
