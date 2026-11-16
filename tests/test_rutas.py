"""Detección por sistema: Windows, macOS y Linux simulados con monkeypatch."""
import plistlib
import subprocess
from pathlib import Path

import pytest

import config
import creality_print as cpm
import impresora as imp
import rutas


@pytest.fixture
def casa(tmp_path, monkeypatch):
    """Un HOME vacío y sin variables que cambien las rutas."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    for var in ("APPDATA", "XDG_CONFIG_HOME", "CREALITY_PRINT_EXE", "CREALITY_PRINT_DATA",
                "CREALITY_PLUGIN_CONFIG", "ProgramFiles"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(rutas.shutil, "which", lambda nombre: None)
    return tmp_path


def con_sistema(monkeypatch, s):
    monkeypatch.setattr(rutas, "sistema", lambda: s)


def datos_de_version(base: Path, version: str) -> Path:
    d = base / version
    d.mkdir(parents=True)
    (d / "Creality.conf").write_text('{"app": {"version": "%s"}}\n# MD5 checksum 0\n' % version, encoding="utf-8")
    return d


def app_mac(base: Path, version: str, nombre="Creality Print.app") -> Path:
    app = base / nombre
    (app / "Contents" / "MacOS").mkdir(parents=True)
    exe = app / "Contents" / "MacOS" / "CrealityPrint"
    exe.write_text("")
    with open(app / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump({"CFBundleExecutable": "CrealityPrint", "CFBundleShortVersionString": version}, f)
    return exe


@pytest.mark.parametrize("plataforma,esperado", [
    ("win32", "windows"), ("darwin", "mac"), ("linux", "linux"), ("freebsd14", "linux")])
def test_sistema(monkeypatch, plataforma, esperado):
    monkeypatch.setattr(rutas.sys, "platform", plataforma)
    assert rutas.sistema() == esperado


# ------------------------------------------------------------- carpetas

def test_datos_windows(casa, monkeypatch):
    con_sistema(monkeypatch, "windows")
    monkeypatch.setenv("APPDATA", str(casa / "Roaming"))
    assert rutas.carpetas_datos() == [casa / "Roaming" / "Creality" / "Creality Print"]
    assert rutas.carpeta_plugin() == casa / "Roaming" / "creality-print-plugin"
    assert config.ruta() == casa / "Roaming" / "creality-print-plugin" / "config.json"


def test_datos_mac(casa, monkeypatch):
    con_sistema(monkeypatch, "mac")
    soporte = casa / "Library" / "Application Support"
    assert rutas.carpetas_datos()[0] == soporte / "Creality" / "Creality Print"
    assert config.ruta() == soporte / "creality-print-plugin" / "config.json"


def test_datos_linux_xdg(casa, monkeypatch):
    con_sistema(monkeypatch, "linux")
    assert rutas.carpetas_datos()[0] == casa / ".config" / "Creality" / "Creality Print"
    assert rutas.carpetas_datos()[1] == (casa / ".var" / "app" / rutas.FLATPAK_ID / "config"
                                         / "Creality" / "Creality Print")
    assert config.ruta() == casa / ".config" / "creality-print-plugin" / "config.json"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(casa / "xdg"))
    assert rutas.carpetas_datos()[0] == casa / "xdg" / "Creality" / "Creality Print"
    assert rutas.carpeta_plugin() == casa / "xdg" / "creality-print-plugin"


def test_buscar_datos_coge_la_version_mas_alta(casa, monkeypatch):
    con_sistema(monkeypatch, "mac")
    base = rutas.carpetas_datos()[0]
    datos_de_version(base, "7.2")
    nueva = datos_de_version(base, "7.3")
    (base / "7.4").mkdir()  # sin Creality.conf: nunca se ha abierto
    assert cpm._buscar_datos() == nueva


def test_buscar_datos_flatpak(casa, monkeypatch):
    con_sistema(monkeypatch, "linux")
    d = datos_de_version(rutas.carpetas_datos()[1], "7.3")
    assert cpm._buscar_datos() == d


def test_sin_datos_dice_donde_ha_mirado(casa, monkeypatch):
    con_sistema(monkeypatch, "linux")
    with pytest.raises(cpm.ErrorCrealityPrint, match=r"\.config"):
        cpm._buscar_datos()


def test_hosts_de_creality_print_en_mac(casa, monkeypatch):
    con_sistema(monkeypatch, "mac")
    d = datos_de_version(rutas.carpetas_datos()[0], "7.3")
    (d / "deviceInfo.json").write_text(
        '{"groups": [{"list": [{"address": "10.0.0.5", "model": "F021"}]}]}', encoding="utf-8")
    assert imp._hosts_de_creality_print() == [("10.0.0.5", "F021")]


# ------------------------------------------------------------- ejecutable

def test_exe_mac_en_applications_del_usuario(casa, monkeypatch):
    con_sistema(monkeypatch, "mac")
    exe = app_mac(casa / "Applications", "7.3.1.6378")
    assert cpm._buscar_exe() == exe
    assert cpm.version_exe(exe) == "7.3.1.6378"


def test_exe_mac_elige_la_version_mas_alta(casa, monkeypatch):
    con_sistema(monkeypatch, "mac")
    app_mac(casa / "Applications", "7.2.2.5483")
    nueva = app_mac(casa / "Applications", "7.3.1.6378", "Creality Print 7.3.app")
    assert cpm._buscar_exe() == nueva


def test_exe_mac_variable_con_el_app(casa, monkeypatch):
    con_sistema(monkeypatch, "mac")
    exe = app_mac(casa / "otra", "7.3")
    monkeypatch.setenv("CREALITY_PRINT_EXE", str(exe.parent.parent.parent))
    assert cpm._buscar_exe() == exe


def test_exe_linux_appimage(casa, monkeypatch):
    con_sistema(monkeypatch, "linux")
    (casa / "Applications").mkdir()
    vieja = casa / "Applications" / "CrealityPrint-V7.2.2.5483-x86_64-Release.AppImage"
    nueva = casa / "Applications" / "CrealityPrint-V7.3.1.6378-x86_64-Release.AppImage"
    vieja.write_text("")
    nueva.write_text("")
    assert cpm._buscar_exe() == nueva
    assert cpm.version_exe(nueva) == "7.3.1.6378"


def test_exe_linux_flatpak(casa, monkeypatch):
    con_sistema(monkeypatch, "linux")
    exe = casa / ".local" / "share" / "flatpak" / "exports" / "bin" / rutas.FLATPAK_ID
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    assert cpm._buscar_exe() == exe


def test_exe_linux_en_el_path(casa, monkeypatch):
    con_sistema(monkeypatch, "linux")
    exe = casa / "bin" / "CrealityPrint"
    exe.parent.mkdir()
    exe.write_text("")
    monkeypatch.setattr(rutas.shutil, "which", lambda n: str(exe) if n == "CrealityPrint" else None)
    assert cpm._buscar_exe() == exe


def test_exe_variable_que_no_existe(casa, monkeypatch):
    con_sistema(monkeypatch, "linux")
    monkeypatch.setenv("CREALITY_PRINT_EXE", str(casa / "no.AppImage"))
    with pytest.raises(cpm.ErrorCrealityPrint, match="doesn't exist"):
        cpm._buscar_exe()


@pytest.mark.parametrize("s,pista", [("mac", "Applications"), ("linux", "AppImage"), ("windows", "CrealityPrint.exe")])
def test_sin_exe_explica_que_hacer(casa, monkeypatch, s, pista):
    con_sistema(monkeypatch, s)
    monkeypatch.setenv("ProgramFiles", str(casa / "PF"))
    monkeypatch.setattr(cpm, "_exes_del_registro", lambda: [])
    with pytest.raises(cpm.ErrorCrealityPrint, match=pista):
        cpm._buscar_exe()


def test_exe_windows_program_files(casa, monkeypatch):
    con_sistema(monkeypatch, "windows")
    monkeypatch.setenv("ProgramFiles", str(casa / "PF"))
    monkeypatch.setattr(cpm, "_exes_del_registro", lambda: [])
    for v in ("7.0", "6.0"):
        d = casa / "PF" / "Creality" / f"Creality Print {v}"
        d.mkdir(parents=True)
        (d / "CrealityPrint.exe").write_text("")
    assert cpm._buscar_exe() == casa / "PF" / "Creality" / "Creality Print 7.0" / "CrealityPrint.exe"


# ------------------------------------------------------------- lo de alrededor

def test_perfiles_de_sistema_del_bundle_mac(casa, monkeypatch):
    exe = app_mac(casa / "Applications", "7.3")
    cp = cpm.CrealityPrint(exe=exe, datos=casa / "vacia")
    assert cp.dir_sistema == exe.parent.parent / "Resources" / "profiles" / "Creality"
    assert cp.version == "7.3"  # sin Creality.conf, la del Info.plist
    assert cp.prefijo_cli == ["--cli"]


def test_sin_stl_thumb_no_hay_render(casa, monkeypatch):
    exe = app_mac(casa / "Applications", "7.3")
    cp = cpm.CrealityPrint(exe=exe, datos=casa)
    assert cp.stl_thumb is None
    stl = casa / "a.stl"
    stl.write_text("solid a\nendsolid a\n")
    assert cpm.miniatura_base(cp, stl, "#FFFFFF", 1) is None


def test_stl_thumb_del_path(casa, monkeypatch):
    exe = app_mac(casa / "Applications", "7.3")
    monkeypatch.setattr(cpm.shutil, "which", lambda n: "/usr/local/bin/stl-thumb" if n == "stl-thumb" else None)
    assert cpm.CrealityPrint(exe=exe, datos=casa).stl_thumb == Path("/usr/local/bin/stl-thumb")


@pytest.mark.parametrize("s,cmd", [
    ("windows", ["tasklist", "/FI", "IMAGENAME eq CrealityPrint.exe", "/NH"]),
    ("mac", ["pgrep", "-x", "CrealityPrint"]),
    ("linux", ["pgrep", "-x", "CrealityPrint"]),
])
def test_proceso_abierto_por_sistema(monkeypatch, s, cmd):
    con_sistema(monkeypatch, s)
    llamadas = []

    def falso(c, **kw):
        llamadas.append(c)
        salida = "CrealityPrint.exe  1234 Console" if s == "windows" else "1234\n"
        return subprocess.CompletedProcess(c, 0, stdout=salida)

    monkeypatch.setattr(cpm.subprocess, "run", falso)
    assert cpm._proceso_abierto() is True
    assert llamadas == [cmd]


def test_proceso_abierto_sin_pgrep(monkeypatch):
    con_sistema(monkeypatch, "linux")

    def falla(c, **kw):
        raise FileNotFoundError(c[0])

    monkeypatch.setattr(cpm.subprocess, "run", falla)
    assert cpm._proceso_abierto() is False


def test_abrir_en_mac_usa_open(casa, monkeypatch):
    con_sistema(monkeypatch, "mac")
    exe = app_mac(casa / "Applications", "7.3")
    app = str(exe.parent.parent.parent)
    f = casa / "a.stl"
    assert cpm._comando_abrir(exe, f, False) == ["open", "-n", "-a", app, "--args", str(f)]
    assert cpm._comando_abrir(exe, f, True) == ["open", "-a", app, str(f)]


def test_abrir_en_linux_y_windows_lanza_el_exe(casa, monkeypatch):
    exe = casa / "CrealityPrint.AppImage"
    f = casa / "a.stl"
    for s in ("linux", "windows"):
        con_sistema(monkeypatch, s)
        assert cpm._comando_abrir(exe, f, False) == [str(exe), str(f)]
        assert cpm._comando_abrir(exe, f, True) == [str(exe), "--single-instance", str(f)]


def test_abrir_no_usa_flags_de_windows_fuera_de_windows(casa, monkeypatch):
    con_sistema(monkeypatch, "linux")
    vistos = {}
    monkeypatch.setattr(cpm.subprocess, "Popen", lambda cmd, **kw: vistos.update(kw))
    f = casa / "a.stl"
    f.write_text("")
    cpm.abrir(cpm.CrealityPrint(exe=casa / "CrealityPrint", datos=casa), f)
    assert "creationflags" not in vistos and vistos["start_new_session"] is True


def test_config_antigua_fuera_de_windows_se_sigue_leyendo(casa, monkeypatch):
    con_sistema(monkeypatch, "linux")
    antigua = casa / "creality-print-plugin" / "config.json"
    antigua.parent.mkdir()
    antigua.write_text('{"impresora": {"host": "10.0.0.9"}}', encoding="utf-8")
    assert config.ruta() == antigua
    assert config.impresora_guardada() == ("10.0.0.9", "")
