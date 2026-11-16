"""Dónde vive cada cosa en Windows, macOS y Linux.

Creality Print guarda sus datos en `<raíz>/Creality Print/<versión>` (set_data_dir en
src/libslic3r/utils.cpp), y la raíz sale de GUI_App::init_app_config y de
initialize_cli_data_directory (src/slic3r/CLI/CLI.cpp):

- Windows: %APPDATA%\\Creality
- macOS:   ~/Library/Application Support/Creality
- Linux:   $XDG_CONFIG_HOME/Creality (si no, ~/.config/Creality); con Flatpak, el
  XDG_CONFIG_HOME del sandbox: ~/.var/app/io.github.crealityofficial.CrealityPrint/config

El programa: CrealityPrint.exe en Windows; "Creality Print.app/Contents/MacOS/CrealityPrint"
en macOS (el .dmg oficial); en Linux un AppImage, el Flatpak o un `CrealityPrint` en el PATH.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

FLATPAK_ID = "io.github.crealityofficial.CrealityPrint"
CARPETA_APP = Path("Creality") / "Creality Print"


def sistema() -> str:
    """'windows', 'mac' o 'linux' (cualquier otro Unix cuenta como Linux)."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "mac"
    return "linux"


def _appdata() -> Path:
    return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")


def _xdg_config() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def carpetas_datos() -> list[Path]:
    """Las carpetas `.../Creality/Creality Print` posibles, la normal primero.

    Dentro hay una por versión (7.2, 7.3…) con Creality.conf, los perfiles y deviceInfo.json.
    """
    s = sistema()
    if s == "windows":
        return [_appdata() / CARPETA_APP]
    if s == "mac":
        soporte = Path("Library") / "Application Support"
        return [Path.home() / soporte / CARPETA_APP,
                # La versión de la Mac App Store va en sandbox.
                Path.home() / "Library" / "Containers" / "com.creality.crealityprint" / "Data" / soporte / CARPETA_APP]
    return [_xdg_config() / CARPETA_APP,
            Path.home() / ".var" / "app" / FLATPAK_ID / "config" / CARPETA_APP]


def carpeta_plugin() -> Path:
    """Donde guarda el plugin su config.json y el CREALITY.md global."""
    s = sistema()
    if s == "windows":
        return _appdata() / "creality-print-plugin"
    if s == "mac":
        return Path.home() / "Library" / "Application Support" / "creality-print-plugin"
    return _xdg_config() / "creality-print-plugin"


def nombre_proceso() -> str:
    return "CrealityPrint.exe" if sistema() == "windows" else "CrealityPrint"


def candidatos_exe() -> list[Path]:
    """Ejecutables de Creality Print que existen, sin el registro de Windows."""
    s = sistema()
    candidatos: list[Path] = []
    if s == "windows":
        for base in {os.environ.get("ProgramFiles", r"C:\Program Files"), r"C:\Program Files"}:
            candidatos += Path(base, "Creality").glob("Creality Print*/CrealityPrint.exe")
    elif s == "mac":
        for base in (Path("/Applications"), Path.home() / "Applications"):
            for app in base.glob("*Creality*Print*.app"):
                candidatos.append(app / "Contents" / "MacOS" / "CrealityPrint")
    else:
        for nombre in ("CrealityPrint", "crealityprint", "CrealityPrint.AppImage"):
            ruta = shutil.which(nombre)
            if ruta:
                candidatos.append(Path(ruta))
        for base in (Path.home() / "Applications", Path.home() / "Downloads", Path.home() / ".local" / "bin",
                     Path.home() / "AppImages", Path("/opt")):
            candidatos += base.glob("CrealityPrint*.AppImage")
            candidatos += base.glob("Creality*Print*/CrealityPrint*.AppImage")
            candidatos += base.glob("Creality*Print*/bin/CrealityPrint")
        for base in (Path.home() / ".local" / "share" / "flatpak", Path("/var/lib/flatpak")):
            candidatos.append(base / "exports" / "bin" / FLATPAK_ID)
    return [c for c in dict.fromkeys(candidatos) if c.is_file()]
