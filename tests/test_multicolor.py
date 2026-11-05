"""Colores: filamento por objeto, cambio por altura y mezclas (filamentos virtuales)."""
import re
import zipfile
from pathlib import Path

import pytest

import creality_print as cpm
from test_creality_print import CP, FILAMENTO, MAQUINA, PROCESO, _3mf_plano, cubo_stl, instalado

NEGRO = "CR-PLA @Creality K2 0.4 nozzle"


def herramientas_por_altura(ruta):
    """[(z, 'T1'), …] en el orden del gcode."""
    z, eventos = 0.0, []
    for linea in open(ruta, encoding="utf-8"):
        m = re.match(r"G1 Z([\d.]+)", linea)
        if m:
            z = float(m.group(1))
        elif re.match(r"^T\d+\s*$", linea):
            eventos.append((z, linea.strip()))
    return eventos


def test_stl_a_3mf(tmp_path):
    ruta = cpm.stl_a_3mf(cubo_stl(tmp_path / "cubo.stl"), tmp_path / "cubo.3mf")
    objetos = cpm.objetos_3mf(ruta)
    assert [o["name"] for o in objetos] == ["cubo"]
    with zipfile.ZipFile(ruta) as z:
        assert z.read("3D/3dmodel.model").count(b"<vertex ") == 8


def test_asignar_por_nombre_y_cambios_altura(tmp_path):
    r = cpm.preparar_multicolor(_3mf_plano(tmp_path / "a.3mf"), asignar={"texto": 3},
                                cambios_altura=[{"z": 3, "extruder": 2}])
    assert {o["name"]: o["extruder"] for o in r["objects"]} == {"Base": 1, "Texto": 3}
    assert r["extruders_used"] == [1, 2, 3]
    with zipfile.ZipFile(r["file"]) as z:
        xml = z.read("Metadata/layer_config_ranges.xml").decode()
    # Un rango de altura por objeto (los ve la ventana de Creality Print), no un tool_change.
    assert xml.count('<range min_z="3" max_z="1000">') == 2
    assert '<option opt_key="extruder">2</option>' in xml


def test_cambio_altura_respeta_rangos_y_quita_tool_change(tmp_path):
    ruta = _3mf_plano(tmp_path / "a.3mf")
    with zipfile.ZipFile(ruta, "a") as z:
        z.writestr("Metadata/project_settings.config", '{"layer_height": "0.08"}')
        z.writestr("Metadata/layer_config_ranges.xml",
                   '<objects><object id="1"><range min_z="0.16" max_z="0.48">'
                   '<option opt_key="extruder">0</option><option opt_key="layer_height">0.16</option>'
                   '</range></object></objects>')
        z.writestr("Metadata/custom_gcode_per_layer.xml",
                   '<custom_gcodes_per_layer><plate><plate_info id="1"/>'
                   '<layer top_z="0.8" type="2" extruder="2" color="" extra="" gcode="tool_change"/>'
                   '<layer top_z="5" type="1" extruder="1" color="" extra="" gcode="M600"/></plate></custom_gcodes_per_layer>')
    r = cpm.preparar_multicolor(ruta, cambios_altura=[{"z": 0.48, "extruder": 2}])
    with zipfile.ZipFile(r["file"]) as z:
        rangos = cpm._leer_rangos(z.read("Metadata/layer_config_ranges.xml").decode())
        capas = z.read("Metadata/custom_gcode_per_layer.xml").decode()
    assert [(x["min"], x["max"], x["opciones"]) for x in rangos["1"]] == [
        (0.16, 0.48, {"extruder": "0", "layer_height": "0.16"}),
        (0.48, 1000.0, {"extruder": "2", "layer_height": "0.08"})]
    assert "tool_change" not in capas and "M600" in capas
    assert cpm.info_3mf(Path(r["file"]))["num_filamentos"] == 2  # los cuenta por los rangos


def test_rangos_con_capa():
    xml = '<range min_z="1" max_z="2">\n   <option opt_key="extruder">2</option>\n  </range>'
    assert 'opt_key="layer_height">0.2<' in cpm._rangos_con_capa(xml, "0.2")
    con = xml.replace("</range>", '<option opt_key="layer_height">0.1</option></range>')
    assert cpm._rangos_con_capa(con, "0.2") == con


def test_objeto_inexistente_lista_los_que_hay(tmp_path):
    with pytest.raises(cpm.ErrorCrealityPrint, match="Base"):
        cpm.preparar_multicolor(_3mf_plano(tmp_path / "a.3mf"), asignar={"tapa": 2})


def test_filas_mezcla():
    assert cpm.filas_mezcla([{"a": 1, "b": 2}], 2) == "1,2,1,1,50,0,g,w,m0,d0,o0,u1"
    assert cpm.filas_mezcla([{"a": 2, "b": 1, "porcentaje_b": 25, "modo": "puntos"}, {}], 3) == \
        "2,1,1,1,25,0,g,w,m1,d0,o0,u1;1,2,1,1,50,0,g,w,m0,d0,o0,u2"
    with pytest.raises(cpm.ErrorCrealityPrint):
        cpm.filas_mezcla([{"a": 1, "b": 3}], 2)
    with pytest.raises(cpm.ErrorCrealityPrint):
        cpm.filas_mezcla([{"modo": "arcoiris"}], 2)


@instalado
def test_cambio_de_color_por_altura_en_un_stl(tmp_path):
    r = cpm.preparar_multicolor(cubo_stl(tmp_path / "cubo.stl"), cambios_altura=[{"z": 10, "extruder": 2}])
    res = cpm.laminar(CP, r["file"], MAQUINA, PROCESO, [FILAMENTO, NEGRO],
                      ajustes={"enable_prime_tower": 0}, modelo_esperado="Creality K2", cama=(260, 260, 260))
    g = res.gcodes[0]
    assert g["ok_to_print"], g["problems"]
    cambios = [(z, t) for z, t in herramientas_por_altura(g["file"]) if t == "T1"]
    assert len(cambios) == 1 and 9.5 < cambios[0][0] < 10.5


@instalado
@pytest.mark.xfail(CP is not None and cpm._version_tupla(CP.version) >= (7, 3), strict=True,
                   reason="Creality Print 7.3 escribe el extrusor virtual (T2) y aborta con -100")
def test_objeto_en_una_mezcla_alterna_capas(tmp_path):
    r = cpm.preparar_multicolor(cubo_stl(tmp_path / "cubo.stl"), asignar={"cubo": 3})
    res = cpm.laminar(CP, r["file"], MAQUINA, PROCESO, [FILAMENTO, NEGRO], mezclas=[{"a": 1, "b": 2}],
                      ajustes={"enable_prime_tower": 0}, modelo_esperado="Creality K2", cama=(260, 260, 260))
    herramientas = [t for _, t in herramientas_por_altura(res.gcodes[0]["file"])]
    assert herramientas.count("T0") > 20 and herramientas.count("T1") > 20
    assert any("mixes" in a for a in res.ajustes_aplicados)


# ---------------------------------------------------------------- relieves (HueForge, carteles)

_CARAS_CAJA = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1),
               (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]


def relieve_stl(ruta):
    """Placa de 40x20x1 (el fondo) con un bloque de 10x10 que sube hasta 2 (el relieve)."""
    partes = ["solid relieve"]
    for x0, y0, z0, x1, y1, z1 in ((0, 0, 0, 40, 20, 1), (10, 5, 1, 20, 15, 2)):
        v = [(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
        for a, b, c in _CARAS_CAJA:
            partes.append("facet normal 0 0 0\nouter loop")
            partes += [f"vertex {v[i][0]} {v[i][1]} {v[i][2]}" for i in (a, b, c)]
            partes.append("endloop\nendfacet")
    partes.append("endsolid relieve")
    ruta.write_text("\n".join(partes) + "\n")
    return ruta


def test_analizar_alturas_sugiere_el_fondo(tmp_path):
    [o] = cpm.analizar_alturas(relieve_stl(tmp_path / "r.stl"))
    assert [s["z"] for s in o["top_surfaces"]] == [1.0, 2.0]
    assert o["suggested_change_z"] == 1.0


def test_altura_de_cambio_ignora_restos_pequenos():
    sup = [{"z": 0.48, "area_mm2": 4450}, {"z": 0.56, "area_mm2": 10}, {"z": 1.2, "area_mm2": 4410}]
    assert cpm.altura_de_cambio(sup) == 0.48
    assert cpm.altura_de_cambio([{"z": 2.0, "area_mm2": 100}]) is None


def test_cambio_auto_en_stl_pone_un_bloque_con_el_filamento_de_abajo(tmp_path):
    r = cpm.preparar_multicolor(relieve_stl(tmp_path / "r.stl"), cambios_altura=[{"z": "auto", "extruder": 2}])
    assert r["height_changes"][0]["changes"] == [{"z": 1.0, "extruder": 2}]
    assert "warnings" not in r
    with zipfile.ZipFile(r["file"]) as z:
        modelo = z.read("3D/3dmodel.model").decode()
        config = z.read("Metadata/model_settings.config").decode()
    assert modelo.count("<component ") == 2
    partes = re.findall(r'<part id="(\d+)".*?key="extruder" value="(\d)"', config, re.S)
    assert [e for _, e in partes] == ["2", "1"]  # la malla entera arriba y el bloque después
    # El bloque llega justo por encima del fondo: z de la cama 1,01.
    [o] = cpm.analizar_alturas(r["file"])
    assert o["top_surfaces"][0]["z"] == 1.0


def test_partes_en_un_proyecto_de_creality(tmp_path):
    from test_creality_print import _proyecto_creality
    r = cpm.preparar_multicolor(_proyecto_creality(tmp_path / "p.3mf"), cambios_altura=[{"z": 5, "extruder": 2}])
    assert "warnings" not in r
    with zipfile.ZipFile(r["file"]) as z:
        sub = z.read("3D/Objects/object_1.model").decode()
        modelo = z.read("3D/3dmodel.model").decode()
        config = z.read("Metadata/model_settings.config").decode()
    assert '<object id="3" type="model">' in sub
    assert 'p:path="/3D/Objects/object_1.model" objectid="3"' in modelo
    assert re.findall(r'<part id="(\d+)".*?key="extruder" value="(\d)"', config, re.S) == [("1", "2"), ("3", "1")]
    # El cubo de 20 está centrado en z=10 sobre la cama: el bloque va de -10 a -4,99 en el objeto.
    assert 'z="-4.99000"' in sub


def test_sin_planta_rectangular_avisa_y_deja_solo_el_rango(tmp_path):
    r = cpm.preparar_multicolor(_3mf_plano(tmp_path / "a.3mf"), cambios_altura=[{"z": 3, "extruder": 2}])
    assert len(r["warnings"]) == 2 and "rectangle" in r["warnings"][0]
    with zipfile.ZipFile(r["file"]) as z:
        assert "<component" not in z.read("3D/3dmodel.model").decode()


def test_auto_sin_fondo_da_error(tmp_path):
    with pytest.raises(cpm.ErrorCrealityPrint, match="auto"):
        cpm.preparar_multicolor(cubo_stl(tmp_path / "c.stl"), cambios_altura=[{"z": "auto", "extruder": 2}])


@instalado
def test_relieve_en_dos_colores_lamina_con_el_cambio_en_el_fondo(tmp_path):
    r = cpm.preparar_multicolor(relieve_stl(tmp_path / "r.stl"), cambios_altura=[{"z": "auto", "extruder": 2}])
    res = cpm.laminar(CP, r["file"], MAQUINA, PROCESO, [FILAMENTO, NEGRO],
                      ajustes={"enable_prime_tower": 0}, modelo_esperado="Creality K2", cama=(260, 260, 260))
    g = res.gcodes[0]
    assert g["ok_to_print"], g["problems"]
    cambios = [(z, t) for z, t in herramientas_por_altura(g["file"]) if t == "T1"]
    assert len(cambios) == 1 and 1.0 <= cambios[0][0] < 1.2  # al acabar la capa del fondo
