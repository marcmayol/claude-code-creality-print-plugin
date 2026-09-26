---
# Copia este archivo como CREALITY.md en la carpeta de tus modelos (vale para ella y
# todo lo que tenga dentro) o en %APPDATA%\creality-print-plugin\ (vale siempre).
# Todo es opcional: pon solo lo que quieras fijar. Manda el CREALITY.md más cercano
# al modelo, y lo que pidas en la conversación manda sobre todos.

maquina: Creality K2 0.4 nozzle
proceso: 0.20mm Standard @Creality K2 0.4 nozzle
filamentos:                      # uno por extrusor (T0, T1…)
  - Hyper PLA @Creality K2 0.4 nozzle
ajustes:                         # claves de los perfiles de Creality Print
  wall_loops: 3
  sparse_infill_density: 20%
  brim_type: outer_only
carpeta_salida: gcode            # relativa a este archivo
ranuras:                         # extrusor -> ranura del CFS que Claude propondrá
  "1": 1D
---

# Soportes para la pared

Texto libre: Claude lo lee y lo sigue.

- Todo en PLA negro.
- Las piezas que van a la intemperie, con 4 paredes.

## Notas

- En el CREALITY.md global, esta sección la rellena `guardar_nota`.
