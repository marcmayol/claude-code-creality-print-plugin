<p align="center"><img src="assets/banner.png" alt="Plugin de Claude Code para Creality Print y las impresoras Creality" width="100%"></p>

# claude-code-creality-print-plugin

[English](README.md) · **Español**

Plugin de [Claude Code](https://code.claude.com) para laminar con **Creality Print** y
manejar tu **impresora Creality** (familia K2, familia K1…) hablando con Claude, sin abrir
la ventana y sin OrcaSlicer.

## Qué hace

- **Lamina** STL, 3MF, OBJ y STEP con el motor de Creality Print, con los perfiles que
  tengas seleccionados, los de tu `CREALITY.md` o los que digas, y cambia los ajustes que
  pidas.
- **Revisa cada gcode contra la cama real de la impresora.** Rechaza el que es de otra
  máquina (un perfil de K2 Pro que Creality Print eligió solo en una K2, un 3MF bajado para
  una K1C) y el que saca el cabezal de la cama.
- **Colores:** un filamento por objeto, cambio de filamento a una altura y las **mezclas** de
  Creality Print 7 (filamentos virtuales que alternan dos colores).
- **Pone las miniaturas** para la pantalla de la impresora.
- **Lee** el estado de la impresora, el CFS, los archivos y el historial.
- **Sube gcodes y lanza impresiones como Creality Print:** por el websocket 9999 manda
  `colorMatch` (qué extrusor sale de qué ranura del CFS) y `multiColorPrint`. Así se elige la
  ranura, cosa que por Moonraker no se puede. Sin CFS, bobina externa.
- **Se acuerda de lo tuyo** en un `CREALITY.md`, global o por proyecto, como un AGENTS.md.

## Impresoras

Vale para las Creality con **Creality OS**: Klipper con Moonraker (puerto 7125) y el servicio
del puerto 9999 que usa Creality Print. Los modelos y el tamaño de su cama salen de la propia
lista de Creality Print, así que conoce todos los suyos.

| Impresora | Estado |
|---|---|
| K2 (260 mm) con CFS | **Probada** |
| K2 Pro, K2 Plus, K2 SE | Deberían funcionar, sin probar |
| K1, K1C, K1 Max, K1 SE (con o sin CFS-C) | Deberían funcionar, sin probar |
| Otras con Creality OS (Hi, Ender-3 V3 KE…) | Deberían funcionar, sin probar |
| Creality con Marlin (Ender-3 clásicas, CR-10…) | Laminar sí; hablar con la impresora, no |

Si pruebas otra, abre un issue y cuéntalo.

## Herramientas MCP

| Herramienta | Qué hace | ¿Mueve la impresora? |
|---|---|---|
| `setup_printer` | Busca la impresora (Creality Print o red local) y la guarda | no |
| `get_presets`, `save_presets` | Los `CREALITY.md` que aplican a un modelo; crearlos o cambiarlos | no |
| `list_notes`, `add_note`, `delete_note` | Tus reglas, en el `CREALITY.md` global | no |
| `creality_print_info` | Instalación, versión, perfiles seleccionados, impresora de destino | no |
| `list_profiles` | Perfiles de máquina, proceso o filamento (solo los de tu máquina) | no |
| `get_profile_settings` | Claves y valores de un perfil ya resuelto | no |
| `list_3mf_objects`, `prepare_multicolor` | Objetos de un 3MF; colores por objeto y por altura | no |
| `slice` | Lamina y revisa; también mezclas. Devuelve `ok_to_print`, problemas, tiempo, gramos, extrusores | no |
| `analyze_gcode` | Revisa un gcode local o de la impresora | no |
| `open_in_creality_print` | Abre un archivo en una ventana para mirarlo | no |
| `printer_status`, `cfs_status`, `printer_files`, `print_history` | Lectura | no |
| `upload_gcode` | Sube un gcode ya revisado, sin imprimirlo | no |
| `start_print` | Lanza la impresión con las ranuras del CFS o la bobina externa | **sí** (pide `confirm` = nombre) |
| `control_print` | Pausar, reanudar, cancelar | **sí** (cancelar pide `confirm`) |

La skill `creality-print` le explica a Claude cómo usarlas y le prohíbe lanzar o cancelar
nada sin preguntar antes. Las herramientas y la skill están en inglés, que es como mejor las
entiende Claude, pero te contesta en español.

## Instalar

Requisitos:

- Windows con **Creality Print 7.2** instalado (probado con la 7.2.2.5483).
- [uv](https://docs.astral.sh/uv/) en el PATH. El servidor declara sus dependencias dentro
  de `server.py` y uv las instala la primera vez.
- La impresora en la misma red. La encuentra sola (en Creality Print o escaneando la red).

```powershell
claude plugin marketplace add marcmayol/claude-code-creality-print-plugin
claude plugin install creality-print@marc-3d
```

Después, en Claude Code: «lamina este STL en PETG con 4 paredes», «¿cómo va la
impresora?», «¿qué hay en el CFS?», «haz la base blanca y las letras negras a partir de 3 mm».

## Colores

- **Un filamento por objeto**: «el texto en negro y la base en blanco». Claude mira los
  objetos con `list_3mf_objects` y los asigna con `prepare_multicolor`.
- **Cambio de filamento por altura**: «hasta 3 mm blanco y luego negro». Vale también para
  un STL de una pieza: el relieve sale de otro color.
- **Mezclas** de Creality Print 7: filamentos virtuales que alternan dos físicos por capas o
  con tramado, para sacar un tercer tono. Con N filamentos, la mezcla 1 es el extrusor N+1.
  Cada cambio purga, así que tarda y gasta bastante más.
- **Pintar caras sueltas** sigue siendo cosa de la ventana de Creality Print; el 3MF que
  guardes se lamina después respetando los colores.

## Tus datos y tu CREALITY.md

El plugin no lleva nada tuyo dentro: lo que necesita lo averigua y lo guarda en tu PC.

- **La impresora.** La primera vez, `setup_printer` prueba las que tiene Creality
  Print y, si no responde ninguna, escanea tu red local (puertos 9999 y 7125). Guarda su IP
  y su modelo en `%APPDATA%\creality-print-plugin\config.json`; el modelo es lo que se usa
  para comprobar la cama. Si un día cambia de IP, se vuelve a llamar.
- **Tus perfiles y reglas, en `CREALITY.md`.** Le dice al plugin qué usar sin repetirlo cada
  vez: arriba, entre `---`, lo que se aplica solo al laminar; debajo, reglas en texto libre
  que Claude lee y sigue. Plantilla comentada en [`CREALITY.ejemplo.md`](CREALITY.ejemplo.md) (en inglés: [`CREALITY.example.md`](CREALITY.example.md)).

```markdown
---
process: 0.16mm Standard @Creality K2 0.4 nozzle
filaments: [Hyper PLA @Creality K2 0.4 nozzle]
settings:
  wall_loops: 4
  brim_type: outer_only
output_dir: gcode
slots: {"1": 1D}
---
# Soportes para la pared
Todo en PLA negro. Las piezas de exterior, con 4 paredes.
```

- **Global**, en `%APPDATA%\creality-print-plugin\CREALITY.md`: vale siempre. Su sección
  `## Notes` (o `## Notas`) la rellena `add_note` («mi ABS va a 270 °C», «brim en piezas altas»).
- **De proyecto**, en la carpeta de los modelos o en cualquiera de las de encima: vale para
  lo que haya dentro.
- Manda el más cercano al modelo, y lo que pidas en la conversación manda sobre todos. Los
  `settings` se suman de un nivel a otro.
- Claves: `machine`, `process`, `filaments` (un perfil por extrusor), `settings` (claves del
  perfil a cambiar), `output_dir` (relativa al archivo) y `slots` (las ranuras que Claude
  propondrá al imprimir; igualmente te pregunta). Las claves en español de la primera versión
  (`maquina`, `proceso`, `ajustes`…) siguen funcionando.
- Se puede editar a mano o pedírselo a Claude («para esta carpeta usa 0,16 mm y brim»), que
  usa `save_presets` y comprueba antes que los perfiles existen, que son de tu
  impresora y que los ajustes son claves reales.

Variables opcionales, por si prefieres fijarlo a mano:

- `CREALITY_HOST`: IP u hostname de la impresora (antes `K2_HOST`, que sigue valiendo).
  Manda sobre todo lo demás.
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
  - Colores: el extrusor de cada objeto va en `Metadata/model_settings.config`, los cambios
    por altura en `Metadata/custom_gcode_per_layer.xml` y las mezclas en
    `mixed_filament_definitions` (formato de `MixedFilament.cpp` de Creality Print 7.2).
- `gcode.py`: revisa el gcode. Solo cuenta los movimientos de las capas: la purga del
  arranque y la rampa de desecho del cambio de color son de la máquina.
- `impresora.py`: Moonraker (7125) para leer y subir; websocket 9999 para lanzar y
  controlar, con los mismos mensajes que `resources/web/deviceMgr` de Creality Print. La
  carpeta de gcodes se la pregunta a Moonraker (en la K2 es `/mnt/UDISK/…`, en la K1 otra).
- `config.py`: la impresora guardada, fuera del repo; incluye la búsqueda en la red local.
- `preconfig.py`: los `CREALITY.md` (global y de proyecto), su combinación y las notas.
- `server.py`: el servidor MCP (FastMCP, stdio).

## Tests

```powershell
python -m pytest -q
```

Los que necesitan Creality Print o la impresora se saltan si no están. Los de la impresora
solo leen: ningún test sube, imprime ni pausa nada.

## Estado y límites

- Probado con una K2 (base, 260 mm) con un CFS; el resto de modelos, sin probar.
- `start_print` ya ha lanzado impresiones reales en la K2 con el mapeo de ranuras del CFS. El
  protocolo sale del código de Creality Print y del mapeo que la K2 guarda de sus trabajos.
- Creality Print 7.3 cambia la CLI: pide `--cli` y `--need-gcode-file`, sin el cual lamina, sale
  con código 0 y borra el gcode. El plugin antepone los dos si detecta la 7.3 (probado en la
  7.3.0.6151). La 7.3 tampoco admite `--load-settings` con un proyecto 3MF, así que el plugin
  mete los perfiles elegidos en una copia del proyecto.
- **Fallo conocido en la 7.3: los filamentos mezclados (`mixes`) no laminan.** Creality Print
  carga la mezcla pero sigue escribiendo el extrusor virtual (`Invalid T command (T2)`) y aborta
  con código -100. En la 7.2 funcionaban.

Proyecto personal, sin relación con Creality. Creality y Creality Print son marcas de
Shenzhen Creality 3D Technology. Imprimir mueve una máquina con piezas calientes: revisa lo
que lanzas.

## Licencia

MIT
