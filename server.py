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
"""MCP server: Creality Print + Creality printers (K2, K1…) for Claude Code.

Slices with Creality Print's own CLI (not Orca), checks every gcode before it reaches the
printer and drives the printer the way Creality Print does: Moonraker to read and upload,
the port 9999 websocket to start jobs on the CFS.

Everything that moves the printer (start_print, cancelling) needs an explicit confirmation
with the file name, and the skill tells Claude to ask the person first.
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
_printer: imp.Impresora | None = None


def cp() -> cpm.CrealityPrint:
    global _cp
    if _cp is None:
        _cp = cpm.CrealityPrint.detectar()
    return _cp


def printer() -> imp.Impresora:
    global _printer
    if _printer is None:
        _printer = imp.Impresora.detectar()
    return _printer


def target_machine() -> tuple[str, tuple[int, int, int]] | None:
    """Model and bed of the real printer, even if it is switched off right now."""
    try:
        m = printer().modelo
        if m:
            return m
    except imp.ErrorImpresora:
        pass
    saved = config.impresora_guardada()
    if saved and saved[1] in imp.MODELOS:
        return imp.MODELOS[saved[1]]
    for _, code in imp._hosts_de_creality_print():
        if code in imp.MODELOS:
            return imp.MODELOS[code]
    return None


def _check(path: Path) -> gcode.Informe:
    target = target_machine()
    if target:
        return gcode.analizar(path, target[0], target[1])
    report = gcode.analizar(path)
    report.avisos.append("Unknown printer: the model and bounds couldn't be checked against the real machine.")
    return report


def _error(e: Exception) -> dict:
    return {"error": str(e)}


def _at(items: list, i: int):
    return items[i] if 0 <= i < len(items) else None


# ---------------------------------------------------------------- personal setup

@mcp.tool()
def setup_printer(host: str | None = None) -> dict:
    """Find the printer and save it in the plugin's config (on this PC, outside the repo).

    Without `host`: tries the printers saved in Creality Print and, if none answers, scans the
    local network. With `host` (IP or hostname): checks that one and saves it. If more than one
    turns up, returns the list so the person can choose; call again with the chosen host.
    Use it the first time, or when the printer "disappears" (its IP changed via DHCP).
    """
    global _printer
    if host:
        found = [r for r in [config.identificar(host)] if r]
        if not found:
            return {"error": f"There is no Creality printer with Moonraker (port 7125) at {host}."}
    else:
        found = config.buscar_impresoras()
        if not found:
            return {"error": "No Creality printer found on the network. Is it on and on the same network as "
                             "this PC? Ask for its IP (shown on the printer's screen, network settings)."}
    supported = [r for r in found if r["supported"]]
    if len(supported) != 1:
        return {"choose_one": found,
                "reason": "Several printers found: ask which one and call again with its host."
                if len(supported) > 1 else "None of them is a model Creality Print knows."}
    chosen = supported[0]
    config.guardar_impresora(chosen["host"], chosen["model_code"], chosen["name"])
    _printer = None
    return {"saved": chosen, "config_file": str(config.ruta())}


@mcp.tool()
def get_presets(model_or_folder: str | None = None) -> dict:
    """What to use according to the CREALITY.md files (like an AGENTS.md for printing).

    Combines the global one (always applies) with the project ones: the model's folder and the
    folders above it; the closest wins. `values` is what `slice` applies by itself (profiles,
    settings, output folder, slots to suggest); `rules` is the person's free text, notes included,
    which must be read and followed. Call it before slicing or printing, with the model's path.
    """
    try:
        r = preconfig.resolver(model_or_folder)
    except preconfig.ErrorPreconfig as e:
        return _error(e)
    result = {"values": r["valores"], "rules": [{"file": x["archivo"], "text": x["texto"]} for x in r["reglas"]],
              "files": r["archivos"], "global_file": str(preconfig.ruta_global())}
    if not r["archivos"]:
        result["hint"] = ("There is no CREALITY.md. Create one with save_presets (scope 'global' or "
                          "'project') or by hand; see the README.")
    return result


@mcp.tool()
def save_presets(
    scope: str,
    folder: str | None = None,
    machine: str | None = None,
    process: str | None = None,
    filaments: list[str] | None = None,
    settings: dict | None = None,
    output_dir: str | None = None,
    slots: dict[str, str] | None = None,
    remove: list[str] | None = None,
) -> dict:
    """Create or change a CREALITY.md with the profiles and settings to use. Only when asked.

    scope: "global" (always applies) or "project" (applies to `folder` and everything inside;
    `folder` is required). Only what is passed changes; the file's free text is left alone.
    remove: keys to delete, e.g. ["process", "settings.brim_type"]. Before saving it checks that
    the profiles exist, belong to the real printer, and that the settings are real keys.
    """
    if scope == "global":
        path = preconfig.ruta_global()
    elif scope == "project":
        if not folder or not Path(folder).is_dir():
            return {"error": "scope 'project' needs `folder`, an existing folder."}
        path = Path(folder).resolve() / preconfig.NOMBRE
    else:
        return {"error": "scope must be 'global' or 'project'."}
    changes = {k: v for k, v in {"machine": machine, "process": process, "filaments": filaments,
                                 "settings": settings, "output_dir": output_dir, "slots": slots}.items()
               if v is not None}
    try:
        _validate_presets(changes, path)
        data = preconfig.actualizar(path, changes, remove)
    except (preconfig.ErrorPreconfig, cpm.ErrorCrealityPrint, imp.ErrorImpresora) as e:
        return _error(e)
    return {"file": str(path), "values": data}


def _validate_presets(changes: dict, path: Path) -> None:
    """Profiles that exist, belong to the real printer, and settings that are real keys."""
    effective = {**preconfig.resolver(path.parent)["valores"], **changes}
    selected = cp().perfiles_activos
    machine = effective.get("machine") or selected["machine"]
    for kind, name in (("machine", changes.get("machine")), ("process", changes.get("process"))):
        if name:
            cp().resolver_perfil(kind, name)
    for f in changes.get("filaments") or []:
        cp().resolver_perfil("filament", f)
    if changes.get("machine"):
        target = target_machine()
        model = cp().resolver_perfil("machine", machine).get("printer_model")
        if target and model and model != target[0]:
            raise cpm.ErrorCrealityPrint(f"'{machine}' is for a '{model}' and the printer is a '{target[0]}'.")
    for s in (changes.get("slots") or {}).values():
        imp.ranura_a_indices(str(s))
    if changes.get("settings"):
        flat = {
            "machine": [cp().resolver_perfil("machine", machine)],
            "process": [cp().resolver_perfil("process", effective.get("process") or selected["process"])],
            "filament": [cp().resolver_perfil("filament", f)
                         for f in (effective.get("filaments") or selected["filaments"][:1])],
        }
        cpm.aplicar_ajustes(flat, changes["settings"])


@mcp.tool()
def list_notes() -> dict:
    """The person's notes (rules worth always remembering) and the saved printer. They live in the
    notes section of the global CREALITY.md; get_presets already includes them."""
    return {
        "printer": config.leer().get("impresora"),
        "notes": [{"number": i + 1, "text": n} for i, n in enumerate(preconfig.notas())],
        "file": str(preconfig.ruta_global()),
    }


@mcp.tool()
def add_note(text: str) -> dict:
    """Save a lasting rule or preference, e.g. "My high-speed ABS prints at 270 °C" or "Parts over
    100 mm get a 5 mm brim". Goes to the global CREALITY.md. Only when asked or confirmed."""
    try:
        return {"notes": [{"number": i + 1, "text": n} for i, n in enumerate(preconfig.anadir_nota(text))]}
    except preconfig.ErrorPreconfig as e:
        return _error(e)


@mcp.tool()
def delete_note(number: int) -> dict:
    """Delete the note with that number (as given by list_notes)."""
    try:
        return {"notes": [{"number": i + 1, "text": n} for i, n in enumerate(preconfig.quitar_nota(number))]}
    except preconfig.ErrorPreconfig as e:
        return _error(e)


# ---------------------------------------------------------------- Creality Print

@mcp.tool()
def creality_print_info() -> dict:
    """Creality Print install: version, folders, profiles selected right now in the app, the
    target printer (model and bed) and the number of saved notes. Start here if anything fails."""
    try:
        info = cp().info()
    except cpm.ErrorCrealityPrint as e:
        return _error(e)
    target = target_machine()
    info["target_printer"] = {"model": target[0], "bed_mm": target[1]} if target else None
    info["saved_notes"] = len(preconfig.notas())
    return info


@mcp.tool()
def list_profiles(kind: str, filter: str = "", machine: str | None = None) -> dict:
    """Creality Print profiles that can be chosen.

    kind: "machine", "process" or "filament". filter: words that must all appear, e.g. "PLA" or
    "0.16mm". Process and filament profiles are limited to those for `machine` (by default the
    one selected in Creality Print, e.g. "Creality K2 0.4 nozzle"); machine="" shows them all.
    Profiles with source "user" are the person's own.
    """
    try:
        profiles = cp().listar_perfiles(kind, filter, machine)
    except cpm.ErrorCrealityPrint as e:
        return _error(e)
    return {"total": len(profiles), "profiles": profiles[:200]}


@mcp.tool()
def get_profile_settings(kind: str, name: str, keys: list[str] | None = None) -> dict:
    """Values of a fully resolved profile (all inheritance applied). Without `keys` it returns
    only the key names; with `keys`, their values. Use it to find a setting's real name before
    changing it in `slice`."""
    try:
        flat = cp().resolver_perfil(kind, name)
    except cpm.ErrorCrealityPrint as e:
        return _error(e)
    if not keys:
        return {"profile": name, "keys": sorted(flat)}
    return {"profile": name, "values": {k: flat.get(k, "(doesn't exist)") for k in keys}}


@mcp.tool()
async def slice(
    model: str,
    machine: str | None = None,
    process: str | None = None,
    filaments: list[str] | None = None,
    settings: dict | None = None,
    output_dir: str | None = None,
    use_3mf_profiles: bool = False,
    arrange: bool = False,
    orient: bool = False,
    mixes: list[dict] | None = None,
) -> dict:
    """Slice an STL, 3MF, OBJ or STEP with Creality Print (its own engine, not Orca) and check the gcode.

    - Without profiles it uses those in the CREALITY.md that applies to the model (get_presets)
      and, if there is none, the ones selected right now in Creality Print. Whatever is passed
      here wins over the CREALITY.md; `settings` are added on top of its settings.
    - filaments: one filament profile per extruder (T0, T1…). For a multicolour 3MF, in the
      order of its filaments.
    - settings: profile keys to change, e.g. {"sparse_infill_density": "20%", "wall_loops": 3,
      "brim_type": "outer_only", "enable_support": 1}. An unknown key is an error (the CLI
      would silently ignore it).
    - A downloaded 3MF carries its author's machine (K1C, K2 Pro…): by default the person's
      profiles are imposed. use_3mf_profiles only when they are known to be right.
    - mixes: virtual filaments alternating two physical ones, e.g.
      [{"a": 1, "b": 2, "percent_b": 50, "mode": "layers"}] (modes: "layers", "dots",
      "simple"). With N filaments, mix 1 is extruder N+1: assign it to an object with
      prepare_multicolor. Every change purges filament: much slower and more waste.
    - arrange/orient: auto-arrange and auto-orient, like the buttons in the app.

    Returns each gcode with its check: `ok_to_print` false means it must NOT be printed. The
    gcode is saved next to the model (or in output_dir) with the name Creality Print would use,
    with thumbnails for the printer's screen.
    """
    target = target_machine()
    try:
        pre = preconfig.resolver(model)
    except preconfig.ErrorPreconfig as e:
        return _error(e)
    v = pre["valores"]
    if use_3mf_profiles:
        v = {k: x for k, x in v.items() if k in ("output_dir", "slots")}
    machine = machine or v.get("machine")
    process = process or v.get("process")
    filaments = filaments or v.get("filaments")
    output_dir = output_dir or v.get("output_dir")
    final_settings = {**v.get("settings", {}), **(settings or {})} or None

    def work():
        return cpm.laminar(
            cp(), model, maquina=machine, proceso=process, filamentos=filaments, salida=output_dir,
            ajustes=final_settings, usar_perfiles_del_3mf=use_3mf_profiles, colocar=arrange,
            orientar=orient, modelo_esperado=target[0] if target else None,
            cama=target[1] if target else None, mezclas=mixes,
        )

    try:
        res = await anyio.to_thread.run_sync(work)
    except cpm.ErrorCrealityPrint as e:
        return _error(e)
    return {
        "gcodes": res.gcodes,
        "profiles": res.perfiles,
        "settings_applied": res.ajustes_aplicados,
        "warnings": res.avisos,
        "seconds": res.segundos,
        "target_printer": target[0] if target else None,
        "presets": {"files": pre["archivos"], "suggested_slots": v.get("slots"),
                    "rules": [{"file": x["archivo"], "text": x["texto"]} for x in pre["reglas"]]}
        if pre["archivos"] else None,
    }


@mcp.tool()
def list_3mf_objects(model: str) -> dict:
    """The objects in a 3MF (name, id and assigned extruder). Use it to know which object gets
    which colour in prepare_multicolor."""
    try:
        return {"objects": cpm.objetos_3mf(Path(model))}
    except (OSError, KeyError, zipfile.BadZipFile) as e:
        return _error(e)


@mcp.tool()
def prepare_multicolor(
    model: str,
    assign: dict[str, int] | None = None,
    height_changes: list[dict] | None = None,
    output: str | None = None,
) -> dict:
    """Build a multicolour 3MF from an STL or a 3MF, leaving the original untouched.

    - assign: {object name or id: extruder}, e.g. {"Text": 2}. Extruder 1 = first filament
      (T0), 2 = second… If `mixes` are sliced afterwards, the mixes are the following numbers
      (with 2 filaments, mix 1 is extruder 3).
    - height_changes: [{"z": 10, "extruder": 2}] switches filament at that height: a base in
      one colour and the raised part in another, like signs and colour lithophanes.

    It can't paint individual faces: that's done by hand in Creality Print. Returns the new
    3MF; then `slice` it with one filament per extruder (and `mixes` if there are virtual ones).
    """
    target = target_machine()
    try:
        return cpm.preparar_multicolor(model, assign, height_changes, output,
                                       target[1][:2] if target else (260, 260))
    except (cpm.ErrorCrealityPrint, OSError, KeyError, ValueError, zipfile.BadZipFile) as e:
        return _error(e)


@mcp.tool()
async def analyze_gcode(file: str) -> dict:
    """Check a gcode: which printer it's for, profiles, filaments/extruders used, time,
    thumbnails and whether the head leaves the printer's REAL bed.

    file: a local path or the name of a file already on the printer. It catches Creality Print
    slicing with the wrong machine profile (e.g. K2 Pro, 300 mm bed, for a K2) without warning.
    """
    path = Path(file)

    def work():
        if path.is_file():
            return _check(path).resumen()
        with tempfile.TemporaryDirectory() as tmp:
            local = printer().descargar(file, Path(tmp) / "g.gcode")
            r = _check(local).resumen()
            r["file"] = f"(on printer) {file}"
            return r

    try:
        return await anyio.to_thread.run_sync(work)
    except (imp.ErrorImpresora, OSError) as e:
        return _error(e)


@mcp.tool()
def open_in_creality_print(path: str, in_open_window: bool = False) -> dict:
    """Open a model, 3MF or gcode in Creality Print so the person can look at it.

    A new window by default. in_open_window=True loads it into the window that is already open,
    which ADDS the model to their current project: ask first."""
    try:
        where = cpm.abrir(cp(), path, in_open_window)
    except cpm.ErrorCrealityPrint as e:
        return _error(e)
    return {"opened": path, "where": where}


# ---------------------------------------------------------------- printer

@mcp.tool()
def printer_status() -> dict:
    """Printer status: idle/printing, file, progress, layer, time left, temperatures and whether
    the bed mesh is loaded. Read-only."""
    try:
        return printer().estado()
    except imp.ErrorImpresora as e:
        return _error(e)


@mcp.tool()
def cfs_status() -> dict:
    """What is in each CFS slot (1A–1D…): material, name, colour, whether it's empty and which
    one is in use, plus the extruder→slot mapping of the last job. Read-only."""
    try:
        c = printer().cfs()
    except imp.ErrorImpresora as e:
        return _error(e)
    for s in c["slots"]:
        s.pop("_color_crudo", None)
    return c


@mcp.tool()
def printer_files(filter: str = "", limit: int = 20) -> dict:
    """Gcode files stored on the printer, newest first. Read-only."""
    try:
        return {"files": printer().archivos(filter, limit)}
    except imp.ErrorImpresora as e:
        return _error(e)


@mcp.tool()
def print_history(limit: int = 10) -> dict:
    """Latest jobs on the printer and how they ended. Note: on a K2 a job that finished fine can
    show as `cancelled` if an axis protects itself at the end. Read-only."""
    try:
        return {"jobs": printer().historial(limit)}
    except imp.ErrorImpresora as e:
        return _error(e)


@mcp.tool()
async def upload_gcode(path: str, name: str | None = None) -> dict:
    """Upload a local gcode to the printer WITHOUT printing it. It is checked first and refused if
    it's not ok to print (another printer, head leaves the bed…)."""
    local = Path(path)
    if not local.is_file():
        return {"error": f"{path} doesn't exist."}

    def work():
        report = _check(local)
        if not report.apto:
            return {"error": "Not uploading: the gcode is not ok to print.", "check": report.resumen()}
        r = printer().subir(local, name)
        r["check"] = {"warnings": report.avisos, "estimated_time": report.tiempo_estimado,
                      "extruders": [t + 1 for t in report.herramientas]}
        return r

    try:
        return await anyio.to_thread.run_sync(work)
    except imp.ErrorImpresora as e:
        return _error(e)


@mcp.tool()
async def start_print(
    name: str,
    confirm: str,
    slots: dict[str, str] | None = None,
    external_spool: bool = False,
    calibrate: bool = False,
) -> dict:
    """STARTS a print of a gcode already on the printer. This moves the machine.

    Only after the person approved it in this conversation, with the file and the slots in
    front of them. `confirm` must be exactly `name`.

    - slots: gcode extruder -> CFS slot, e.g. {"1": "1D"} or {"1": "1A", "2": "1D"}. Required
      with a CFS (see cfs_status and the extruders reported by analyze_gcode). It's the same
      mapping as Creality Print's colour-matching dialog.
    - external_spool: print from the rear spool holder, without the CFS.
    - calibrate: auto-calibration before printing (takes longer).

    Before starting it downloads the gcode from the printer and checks all of it; it won't start
    if the gcode is not ok, the printer is busy, or a slot's material doesn't match.
    """
    if confirm != name:
        return {"error": "Missing confirmation: pass exactly the file name in `confirm`, and only after "
                         "the person has approved it."}
    if slots and external_spool:
        return {"error": "Either CFS slots or the external spool, not both."}

    def work():
        k = printer()
        st = k.estado()
        if st["klipper"] != "ready":
            return {"error": f"Klipper isn't ready ({st['klipper']})."}
        if st["state"] not in imp.LIBRE:
            return {"error": f"The printer is busy ({st['state']}: {st['file']})."}
        ws = k.estado_ws()
        if ws.get("deviceState") not in (0, None):
            return {"error": f"The printer's screen says it's busy (deviceState {ws.get('deviceState')})."}
        with tempfile.TemporaryDirectory() as tmp:
            report = _check(k.descargar(name, Path(tmp) / "g.gcode"))
        if not report.apto:
            return {"error": "Not starting: the gcode is not ok to print.", "check": report.resumen()}
        extruders = [t + 1 for t in report.herramientas]
        types = report.lista("filament_type")
        mapping: dict[int, str] | None = None
        if not external_spool:
            if not slots:
                cfs = k.cfs()
                return {"error": "Slots missing: say which CFS slot feeds each extruder.",
                        "gcode_extruders": [{"extruder": e, "type": _at(types, e - 1),
                                             "colour": _at(report.lista("filament_colour"), e - 1)}
                                            for e in extruders],
                        "cfs": [{k_: v for k_, v in s.items() if k_ != "_color_crudo"} for s in cfs["slots"]]}
            try:
                mapping = {int(e): s for e, s in slots.items()}
            except ValueError:
                return {"error": "The keys of `slots` are extruder numbers: {\"1\": \"1D\"}."}
            missing = [e for e in extruders if e not in mapping]
            if missing:
                return {"error": f"The gcode uses extruders {extruders}; no slot given for {missing}."}
            cfs = {s["slot"]: s for s in k.cfs()["slots"]}
            for e, s in mapping.items():
                box, mat = imp.ranura_a_indices(s)
                slot = cfs.get(f"{box}{'ABCD'[mat]}")
                if slot is None or slot["empty"]:
                    return {"error": f"Slot {s} is empty or doesn't exist."}
                wanted = _at(types, e - 1)
                if wanted and slot["material"] and wanted.upper() != slot["material"].upper():
                    return {"error": f"Extruder {e} is {wanted} and slot {s} holds {slot['material']}. "
                                     "Slice again with the right filament or pick another slot."}
        res = k.imprimir(name, mapping, types, calibrate)
        res["check"] = {"warnings": report.avisos, "estimated_time": report.tiempo_estimado}
        return res

    try:
        return await anyio.to_thread.run_sync(work)
    except imp.ErrorImpresora as e:
        return _error(e)


@mcp.tool()
def control_print(action: str, confirm: str = "") -> dict:
    """Pause, resume or cancel the current print (action: "pause", "resume", "cancel").
    Cancelling can't be undone: it needs confirm="cancel" and the person's approval."""
    if action == "cancel" and confirm != "cancel":
        return {"error": "To cancel, pass confirm=\"cancel\" after the person approves it."}
    try:
        k = printer()
        st = k.estado()
        if st["state"] not in ("printing", "paused"):
            return {"error": f"Nothing is printing ({st['state']})."}
        return k.control(action)
    except imp.ErrorImpresora as e:
        return _error(e)


if __name__ == "__main__":
    mcp.run()
