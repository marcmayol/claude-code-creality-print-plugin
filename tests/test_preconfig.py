"""CREALITY.md: global y de proyecto, precedencia, notas y migración desde config.json."""
import json

import pytest

import config
import preconfig


@pytest.fixture(autouse=True)
def config_temporal(tmp_path, monkeypatch):
    monkeypatch.setenv("CREALITY_PLUGIN_CONFIG", str(tmp_path / "plugin" / "config.json"))


def escribir(ruta, texto):
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(texto, encoding="utf-8")
    return ruta


def test_sin_archivos():
    assert preconfig.resolver(None) == {"valores": {}, "reglas": [], "archivos": []}


def test_manda_el_mas_cercano_y_los_ajustes_se_suman(tmp_path):
    escribir(preconfig.ruta_global(), "---\nproceso: GLOBAL\najustes:\n  wall_loops: 2\n  brim_type: no_brim\n---\nRegla global\n")
    escribir(tmp_path / "proyecto" / "CREALITY.md",
             "---\nproceso: PROYECTO\najustes:\n  brim_type: outer_only\nranuras:\n  1: 1D\n---\n# Proyecto\nTodo negro.\n")
    escribir(tmp_path / "proyecto" / "piezas" / "CREALITY.md", "---\nfilamentos: Hyper PLA\n---\n")
    modelo = escribir(tmp_path / "proyecto" / "piezas" / "soporte.stl", "solid x\nendsolid x\n")

    r = preconfig.resolver(modelo)
    assert r["valores"]["proceso"] == "PROYECTO"
    assert r["valores"]["ajustes"] == {"wall_loops": 2, "brim_type": "outer_only"}
    assert r["valores"]["filamentos"] == ["Hyper PLA"]
    assert r["valores"]["ranuras"] == {"1": "1D"}
    assert [x["texto"] for x in r["reglas"]] == ["Regla global", "# Proyecto\nTodo negro."]
    assert len(r["archivos"]) == 3


def test_carpeta_salida_relativa_al_archivo(tmp_path):
    escribir(tmp_path / "p" / "CREALITY.md", "---\ncarpeta_salida: gcode\n---\n")
    r = preconfig.resolver(tmp_path / "p" / "a.stl")
    assert r["valores"]["carpeta_salida"] == str((tmp_path / "p" / "gcode").resolve())


def test_clave_desconocida_da_error_claro(tmp_path):
    escribir(tmp_path / "CREALITY.md", "---\nwall_loops: 3\n---\n")
    with pytest.raises(preconfig.ErrorPreconfig, match="ajustes"):
        preconfig.resolver(tmp_path)


def test_yaml_roto(tmp_path):
    escribir(tmp_path / "CREALITY.md", "---\najustes: [a: b\n---\n")
    with pytest.raises(preconfig.ErrorPreconfig, match="YAML"):
        preconfig.resolver(tmp_path)


def test_sin_bloque_yaml_es_solo_texto(tmp_path):
    escribir(tmp_path / "CREALITY.md", "Solo reglas, sin perfiles.\n")
    r = preconfig.resolver(tmp_path)
    assert r["valores"] == {}
    assert r["reglas"][0]["texto"] == "Solo reglas, sin perfiles."


def test_actualizar_no_toca_el_texto_y_quita_claves(tmp_path):
    ruta = escribir(tmp_path / "CREALITY.md", "---\nproceso: A\najustes:\n  wall_loops: 2\n---\n# Mis reglas\nNo tocar.\n")
    preconfig.actualizar(ruta, {"ajustes": {"brim_type": "outer_only"}, "carpeta_salida": str(tmp_path / "g")},
                         quitar=["proceso", "ajustes.wall_loops"])
    datos, cuerpo = preconfig.leer(ruta)
    assert "proceso" not in datos
    assert datos["ajustes"] == {"brim_type": "outer_only"}
    assert "carpeta_salida: g" in ruta.read_text(encoding="utf-8")  # se guarda relativa
    assert "# Mis reglas\nNo tocar." in cuerpo


def test_notas_en_el_global():
    preconfig.anadir_nota("Mi ABS va a 270 °C")
    preconfig.anadir_nota("mi abs va a 270 °c")
    preconfig.anadir_nota("Brim en piezas altas")
    assert preconfig.notas() == ["Mi ABS va a 270 °C", "Brim en piezas altas"]
    texto = preconfig.ruta_global().read_text(encoding="utf-8")
    assert "## Notas\n\n- Mi ABS va a 270 °C\n- Brim en piezas altas" in texto
    preconfig.quitar_nota(1)
    assert preconfig.notas() == ["Brim en piezas altas"]
    with pytest.raises(preconfig.ErrorPreconfig):
        preconfig.quitar_nota(9)


def test_notas_conviven_con_perfiles_y_otro_texto():
    escribir(preconfig.ruta_global(), "---\nproceso: X\n---\n# Mías\n\nIntro.\n\n## Otra cosa\n\nNo borrar.\n")
    preconfig.anadir_nota("Nota 1")
    datos, cuerpo = preconfig.leer(preconfig.ruta_global())
    assert datos == {"proceso": "X"}
    assert "Intro." in cuerpo and "No borrar." in cuerpo and "- Nota 1" in cuerpo


def test_migra_notas_de_config_json():
    config.guardar({"impresora": {"host": "h"}, "notas": [{"texto": "Vieja", "fecha": "2026-09-27"}]})
    assert preconfig.notas() == ["Vieja"]
    assert "notas" not in json.loads(config.ruta().read_text(encoding="utf-8"))
    assert config.leer()["impresora"] == {"host": "h"}
