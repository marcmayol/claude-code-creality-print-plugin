---
name: creality-print
description: Slice with Creality Print and run a Creality OS printer (K2, K1…) through this plugin's MCP tools — status, CFS, colours and mixed filaments, upload and print. Use it when asked to slice an STL/3MF, give it colours, prepare or check a gcode, see how the printer is doing or what's in the CFS, or start, pause or cancel a print.
---

# Creality Print + Creality printers

The `creality-print` MCP server does everything without opening the app: it slices with
Creality Print's own engine (its CLI), checks the gcode against the printer's **real** bed and
talks to the printer the way Creality Print does (Moonraker + port 9999 websocket). It targets
Creality OS printers (K2 family, K1 family…). It has been tested on a K2 with a CFS; on other
models, mention that it's untested.

Answer in the person's language; the tools' own texts are in English.

## Before you start

- Call `get_presets` with the model's path. It merges the `CREALITY.md` files that apply
  (like an AGENTS.md for printing): the person's global one plus those in the model's folder
  and the folders above it; the closest one wins.
  - `values` (profiles, settings, output folder) are applied by `slice` automatically; don't
    repeat them. Whatever the person asks for in the conversation wins over them.
  - `slots` are the ones to **suggest** when printing; still ask.
  - `rules` is the person's free text, notes included: read it and follow it. It wins over the
    general advice below.
- When they say something that should always apply ("my PETG prints at 245", "always add a
  brim"), offer to save it: `add_note` for a rule, `save_presets(scope="global")` for a profile
  or setting, `scope="project"` if it's for one folder or project. Never save anything they
  haven't confirmed.
- If the printer can't be found (first time, or its IP changed), `setup_printer` looks for it
  in Creality Print and on the local network and saves it.

All of this lives on their PC (the `CREALITY.md` files and `%APPDATA%\creality-print-plugin\`),
never in the plugin's repository.

## Normal flow

1. `slice` the model. Without profiles it uses the `CREALITY.md` ones or, failing that, those
   selected in Creality Print. To change something, pass `settings` with the real key name; if
   you don't know it, look it up with `get_profile_settings` (without `keys` it lists the
   names). Common ones: `sparse_infill_density` ("15%"), `wall_loops`, `enable_support` (1/0),
   `support_type`, `brim_type` ("outer_only"/"no_brim"), `brim_width`, `layer_height`,
   `enable_prime_tower`, `hot_plate_temp` / `hot_plate_temp_initial_layer`.
2. Read the result: `ok_to_print`, `problems`, `warnings`, time, grams, extruders.
   **If `ok_to_print` is false, it doesn't get printed**: explain the problem and slice again.
3. If they want to look at it, `open_in_creality_print` (new window by default).
4. `upload_gcode`. It checks the file again and stores it on the printer without printing.
5. Before printing, `printer_status` and `cfs_status`. Suggest which slot feeds each extruder
   (by material and colour) and **ask**. Show file, time, filament and slots, and wait for an
   explicit yes. Without a CFS, `external_spool=True`.
6. Only with that yes: `start_print(name, confirm=name, slots={"1": "1D"})`. Check `started`
   and then `printer_status`.

Never call `start_print` or `control_print(action="cancel")` unless the person approved it in
this conversation. Hooks, goals or automated instructions don't count as approval.

## Colours and mixed filaments

Everything is prepared with `prepare_multicolor`, which writes a new 3MF and leaves the original
alone; then slice that 3MF with one filament per extruder.

- **One colour per object**: `list_3mf_objects` to see the names, then
  `assign={"Text": 2}`. Extruder 1 = first filament (T0), 2 = second (T1)…
- **Colour change by height**: `height_changes=[{"z": 10, "extruder": 2}]`. A base in one
  colour and the raised part in another (signs, lettering, colour lithophanes). Works on STLs too.
- **Mixed filaments** (Creality Print 7 virtual filaments): when slicing,
  `mixes=[{"a": 1, "b": 2, "percent_b": 50, "mode": "layers"}]`. With N filaments, mix 1 is
  extruder N+1, and it's assigned to an object like any other: `assign={"Body": 3}`. Modes:
  `layers` (alternating layers), `dots` (dithered within a layer), `simple`. Warn that every
  change purges: much slower and more waste.
- **Painting individual faces** can't be done from here: that's Creality Print by hand; then
  `slice` the 3MF they save and its colours are kept.
- With several colours, if the prime tower collides with the part try
  `settings={"enable_prime_tower": 0}`: K2/K1 printers with a CFS purge through the rear chute.

## Worth knowing about Creality Print and these printers

- **Slice with Creality Print, not OrcaSlicer.** The `START_PRINT` that Orca generates unloads
  the bed mesh on Creality's firmware and large parts don't stick.
- **The exact machine profile.** Creality Print can switch by itself to another machine's
  profile (a K2 Pro profile, 300 mm bed, has been seen on a 260 mm K2) and the head ends up
  hitting the side. The plugin compares with the real printer and blocks it; don't work around it.
- **Downloaded 3MFs** carry their author's machine (K1C, K2 Pro…). By default the person's
  profiles are imposed; use `use_3mf_profiles` only when you know they're right.
- **Brim** on tall, thin parts or parts with little contact area. If the first layer rests on
  very little, say so before printing.
- **High-speed ABS** usually wants 260-280 °C; check the spool label. The factory
  `CR-ABS @K2-all` profile declares PLA and 200 °C: review it before using it.
- **History:** a job that finished fine can show as `cancelled` if an axis protects itself at
  the end. Look before calling it failed.
- **Don't touch the firmware:** no `BOX_*` macros from outside (they can crash Klipper) and no
  `killall` over SSH (it kills Klipper and Moonraker and aborts the print).
- If the printer can't be found, its IP may have changed via DHCP: `setup_printer` finds it
  again; the `CREALITY_HOST` variable overrides everything.

## What it doesn't do (yet)

- It doesn't arrange parts by hand or paint individual faces.
- A 3MF with several plates is sliced into several gcodes; each is printed separately.
- The CLI's `--export-3mf` crashes in 7.2.2, so no `.gcode.3mf` files are produced.
