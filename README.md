<p align="center"><img src="assets/banner.png" alt="creality-print: plugin de Claude Code para Creality Print y la Creality K2" width="100%"></p>

# creality-print — plugin de Claude Code

Laminar con **Creality Print** y manejar una **Creality K2** desde
[Claude Code](https://code.claude.com), sin abrir la ventana y sin OrcaSlicer.

> **English summary:** a Claude Code plugin (MCP server + skill) that slices with Creality
> Print's own CLI, checks every gcode against the printer's *real* bed (catching the
> K2 Pro profile that Creality Print sometimes switches to on its own), adds screen
> thumbnails, and drives a Creality K2 the way Creality Print does: Moonraker to read and
> upload, and the port 9999 websocket (`colorMatch` + `multiColorPrint`) to start jobs on
> the CFS slot you choose. Windows only; docs and tool descriptions are in Spanish.

## Qué hace

- **Lamina** STL, 3MF, OBJ y STEP con el motor de Creality Print. Usa los perfiles que
  tengas seleccionados o los que digas, y cambia los ajustes que pidas.
- **Revisa cada gcode contra la cama real de la impresora.** Rechaza el que es de otra
  máquina (el perfil K2 Pro que Creality Print elige solo, un 3MF bajado para una K1C) y el
  que saca el cabezal de la cama.
- **Pone las miniaturas** para la pantalla de la impresora.
- **Lee** el estado de la K2, el CFS, los archivos y el historial.
- **Sube gcodes y lanza impresiones como Creality Print:** por el websocket 9999 manda
  `colorMatch` (qué extrusor sale de qué ranura del CFS) y `multiColorPrint`. Así se elige
  la ranura, cosa que por Moonraker no se puede.

## Herramientas MCP

| Herramienta | Qué hace | ¿Mueve la impresora? |
|---|---|---|
| `configurar_impresora` | Busca la impresora (Creality Print o red local) y la guarda | no |
| `mis_notas`, `guardar_nota`, `borrar_nota` | Tus reglas y preferencias, guardadas en tu PC | no |
| `creality_print_info` | Instalación, versión, perfiles seleccionados, impresora de destino | no |
| `listar_perfiles` | Perfiles de máquina, proceso o filamento (solo los de tu máquina) | no |
| `ver_ajustes_perfil` | Claves y valores de un perfil ya resuelto | no |
| `laminar` | Lamina y revisa; devuelve `apto`, problemas, tiempo, gramos, extrusores | no |
| `analizar_gcode` | Revisa un gcode local o de la impresora | no |
| `abrir_en_creality_print` | Abre un archivo en una ventana para mirarlo | no |
| `estado_impresora`, `estado_cfs`, `archivos_impresora`, `historial_impresora` | Lectura | no |
| `subir_gcode` | Sube un gcode ya revisado, sin imprimirlo | no |
| `imprimir` | Lanza la impresión con las ranuras del CFS | **sí** (pide `confirmacion` = nombre) |
| `controlar_impresion` | Pausar, reanudar, cancelar | **sí** (cancelar pide `confirmacion`) |

La skill `creality-print` le explica a Claude cómo usarlas y le prohíbe lanzar o cancelar
nada sin preguntar antes.

## Instalar

Requisitos:

- Windows con **Creality Print 7.2** instalado (probado con la 7.2.2.5483).
- [uv](https://docs.astral.sh/uv/) en el PATH. El servidor declara sus dependencias dentro
  de `server.py` y uv las instala la primera vez.
- La impresora añadida en Creality Print (pestaña Dispositivo), o la variable `K2_HOST`.

```powershell
claude plugin marketplace add marcmayol/creality-print-plugin
claude plugin install creality-print@marc-3d
```

Después, en Claude Code: «lamina este STL en PETG con 4 paredes», «¿cómo va la
impresora?», «¿qué hay en el CFS?».

### Tus datos

El plugin no lleva nada tuyo dentro: lo que necesita lo averigua y lo guarda en tu PC, en
`%APPDATA%\creality-print-plugin\config.json`.

- **La impresora.** La primera vez, `configurar_impresora` prueba las que tiene Creality
  Print y, si no responde ninguna, escanea tu red local (puertos 9999 y 7125). Guarda su IP
  y su modelo, que es lo que se usa para comprobar la cama. Si un día cambia de IP, se
  vuelve a llamar.
- **Tus reglas.** Lo que le digas que recuerde («mi ABS va a 270 °C», «brim siempre en piezas
  altas») se guarda con `guardar_nota`. Claude lo lee con `mis_notas` antes de laminar o
  imprimir, y `borrar_nota` lo quita.

Variables opcionales, por si prefieres fijarlo a mano:

- `K2_HOST`: IP u hostname de la impresora. Manda sobre todo lo demás.
- `CREALITY_PRINT_EXE`, `CREALITY_PRINT_DATA`: si Creality Print no está en su sitio.
- `CREALITY_PLUGIN_CONFIG`: otra ruta para el archivo de configuración.

## Cómo funciona por dentro

- `creality_print.py`: busca la instalación, resuelve la herencia de los perfiles (los de
  usuario solo guardan lo que cambian), aplica los ajustes y llama a la CLI.
  - En la 7.2 las opciones van **con guiones** (`--load-settings`); con guion bajo da
    "Invalid option". Como es una app de ventana, `--help` no escribe en la consola.
  - `--export-3mf` revienta en la 7.2.2, así que las miniaturas las pinta `stl-thumb.exe`
    (que trae Creality Print) y se insertan a mano.
  - A los 3MF "planos" (sin datos de Creality) los centra en la cama y les asigna un
    extrusor por material, que es lo que haría la ventana.
- `gcode.py`: revisa el gcode. Solo cuenta los movimientos de las capas: la purga del
  arranque (Y −1) y la rampa de desecho del cambio de color (Y 291,5) son de la máquina.
- `k2.py`: Moonraker (7125) para leer y subir; websocket 9999 para lanzar y controlar, con
  los mismos mensajes que `resources/web/deviceMgr` de Creality Print.
- `config.py`: tu configuración (impresora y notas), fuera del repo; incluye la búsqueda en
  la red local.
- `server.py`: el servidor MCP (FastMCP, stdio).

## Tests

```powershell
python -m pytest -q
```

Los que necesitan Creality Print o la impresora se saltan si no están. Los de la impresora
solo leen: ningún test sube, imprime ni pausa nada.

## Estado y límites

- Probado con una K2 (base, 260 mm) con un CFS. La K2 Plus y la K2 Pro están en la tabla de
  modelos, pero no se han probado.
- **Lanzar trabajos con `imprimir` todavía no está verificado en una impresión real.** El
  protocolo sale del código de Creality Print y del mapeo que la K2 guarda de sus trabajos.
- Creality Print 7.3 cambia la CLI (`--cli`, solo `slice`). El plugin antepone `--cli` si
  detecta la 7.3, pero no está probado.

Proyecto personal, sin relación con Creality. Imprimir mueve una máquina con piezas
calientes: revisa lo que lanzas.

## Licencia

MIT
