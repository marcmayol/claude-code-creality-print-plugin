---
# Copy this file as CREALITY.md into your models' folder (it applies to that folder and
# everything inside) or into %APPDATA%\creality-print-plugin\ (it always applies).
# Everything is optional: set only what you want to pin down. The CREALITY.md closest to
# the model wins, and whatever you ask for in the conversation wins over all of them.
# The keys are in Spanish; the values are Creality Print profile and setting names.

maquina: Creality K2 0.4 nozzle                  # machine profile
proceso: 0.20mm Standard @Creality K2 0.4 nozzle  # process profile
filamentos:                      # filament profiles, one per extruder (T0, T1…)
  - Hyper PLA @Creality K2 0.4 nozzle
ajustes:                         # Creality Print profile keys to change
  wall_loops: 3
  sparse_infill_density: 20%
  brim_type: outer_only
carpeta_salida: gcode            # output folder, relative to this file
ranuras:                         # extruder -> CFS slot that Claude will suggest
  "1": 1D
---

# Wall brackets

Free text: Claude reads it and follows it.

- Everything in black PLA.
- Parts that live outdoors get 4 walls.

## Notas

- In the global CREALITY.md, `guardar_nota` fills in this section.
