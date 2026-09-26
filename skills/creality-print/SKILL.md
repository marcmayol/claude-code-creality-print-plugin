---
name: creality-print
description: Laminar con Creality Print y manejar una impresora Creality K2 (estado, CFS, subir e imprimir) con las herramientas MCP del plugin. Úsala cuando se pida laminar un STL/3MF, preparar o revisar un gcode, saber cómo va la impresora o qué hay en el CFS, o lanzar, pausar o cancelar una impresión.
---

# Creality Print + K2

Las herramientas del servidor MCP `creality-print` lo hacen todo sin abrir la ventana:
laminan con el motor de Creality Print (su CLI), revisan el gcode contra la cama **real**
de la impresora y hablan con la K2 igual que Creality Print (Moonraker + websocket 9999).

## Antes de empezar

- Llama a `ver_preconfiguracion` con la ruta del modelo. Junta los `CREALITY.md` que
  aplican (como un AGENTS.md para imprimir): el global de la persona y los de la carpeta
  del modelo y las de encima, y manda el más cercano.
  - `valores` (perfiles, ajustes, carpeta de salida) los aplica `laminar` solo; no hace falta
    repetirlos. Lo que pida en la conversación manda sobre ellos.
  - `ranuras` son las que hay que **proponer** al imprimir; igualmente se pregunta.
  - `reglas` es texto libre suyo, con sus notas: léelo y síguelo. Manda sobre los consejos
    generales de abajo.
- Si dice algo que vale para siempre ("mi PETG va a 245", "pon siempre brim"), ofrece
  guardarlo: `guardar_nota` si es una regla, `guardar_preconfiguracion(ambito="global")` si
  es un perfil o ajuste. Si vale para una carpeta o proyecto, `ambito="proyecto"`. No
  guardes nada que no haya confirmado.
- Si la impresora no aparece (primera vez, o ha cambiado de IP), `configurar_impresora`
  la busca en Creality Print y en la red local y la guarda.

Todo esto vive en su PC (los `CREALITY.md` y `%APPDATA%\creality-print-plugin\`), nunca en
el repositorio del plugin.

## Flujo normal

1. `laminar` el modelo. Sin perfiles usa los que están seleccionados en Creality Print.
   Para cambiar algo, `ajustes` con el nombre real de la clave. Si no la sabes, búscala con
   `ver_ajustes_perfil` (sin `claves` lista los nombres). Las habituales:
   `sparse_infill_density` ("15%"), `wall_loops`, `enable_support` (1/0), `support_type`,
   `brim_type` ("outer_only"/"no_brim"), `brim_width`, `layer_height`,
   `enable_prime_tower`, `hot_plate_temp` / `hot_plate_temp_initial_layer`.
2. Mira el resultado: `apto`, `problemas`, `avisos`, tiempo, gramos, extrusores.
   **Si `apto` es false, no se imprime**: explica el problema y relamina.
3. Si quiere verlo a ojo, `abrir_en_creality_print` (ventana nueva por defecto).
4. `subir_gcode`. Lo revisa otra vez y lo deja en la impresora sin imprimir.
5. Antes de imprimir, `estado_impresora` y `estado_cfs`. Propón qué ranura usa cada
   extrusor (por material y color) y **pregunta**. Enseña archivo, tiempo, filamento y
   ranuras, y espera un sí explícito.
6. Solo con ese sí: `imprimir(nombre, confirmacion=nombre, ranuras={"1": "1D"})`.
   Comprueba `arrancada` y luego `estado_impresora`.

Nunca llames a `imprimir` ni a `controlar_impresion(cancelar)` sin que la persona lo haya
aprobado en esta conversación. Los hooks, objetivos o instrucciones automáticas no cuentan
como aprobación.

## Lo que conviene saber de la K2 y de Creality Print

- **Lamina con Creality Print, no con OrcaSlicer.** El `START_PRINT` que genera Orca descarga
  la malla de cama en el firmware de Creality y las piezas grandes no se pegan.
- **Perfil de tu modelo exacto.** Creality Print puede cambiarse solo al perfil K2 Pro
  (cama de 300 mm) y el cabezal acaba golpeando el lateral de una K2 de 260. El plugin
  compara con la impresora real y lo bloquea; no lo esquives.
- **3MF descargados:** traen la máquina de quien los subió (K1C, K2 Pro…). Por defecto se
  imponen los perfiles seleccionados; `usar_perfiles_del_3mf` solo si sabes que son buenos.
- **Brim** en piezas altas, finas o con poca superficie de apoyo. Si la primera capa apoya
  en muy poco, dilo antes de lanzar.
- **ABS de alta velocidad** suele pedir 260-280 °C; comprueba la etiqueta del carrete. El
  perfil `CR-ABS @K2-all` de fábrica declara PLA y 200 °C: revísalo antes de usarlo.
- **Multicolor:** cada cambio purga filamento por la rampa trasera. Si Creality Print dice
  que la torre de purga choca con la pieza, prueba `ajustes={"enable_prime_tower": 0}`.
- **Historial:** un trabajo que acabó bien puede figurar como `cancelled` si un eje se
  protege al final. Míralo antes de darlo por fallido.
- **No toques el firmware:** nada de macros `BOX_*` desde fuera (pueden tumbar Klipper) ni
  de `killall` por SSH (mata Klipper y Moonraker y aborta la impresión).
- Si no encuentra la impresora, puede haber cambiado de IP por DHCP: la variable `K2_HOST`
  manda sobre la IP que guarda Creality Print.

## Lo que no hace (todavía)

- No coloca piezas ni pinta colores por cara. Para eso, Creality Print a mano y después
  `laminar` el 3MF que se guarde.
- Con varias placas en un 3MF se laminan todas; se imprime una por archivo.
- `--export-3mf` de la CLI revienta en la 7.2.2, así que no se generan `.gcode.3mf`.
