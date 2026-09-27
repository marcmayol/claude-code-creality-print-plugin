"""Colores: filamento por objeto, cambio por altura y mezclas (filamentos virtuales)."""
import re
import zipfile

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
    assert [o["nombre"] for o in objetos] == ["cubo"]
    with zipfile.ZipFile(ruta) as z:
        assert z.read("3D/3dmodel.model").count(b"<vertex ") == 8


def test_asignar_por_nombre_y_cambios_altura(tmp_path):
    r = cpm.preparar_multicolor(_3mf_plano(tmp_path / "a.3mf"), asignar={"texto": 3},
                                cambios_altura=[{"z": 3, "extrusor": 2}])
    assert {o["nombre"]: o["extrusor"] for o in r["objetos"]} == {"Base": 1, "Texto": 3}
    assert r["extrusores_usados"] == [1, 2, 3]
    with zipfile.ZipFile(r["archivo"]) as z:
        xml = z.read("Metadata/custom_gcode_per_layer.xml").decode()
    assert 'top_z="3" type="2" extruder="2"' in xml


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
    r = cpm.preparar_multicolor(cubo_stl(tmp_path / "cubo.stl"), cambios_altura=[{"z": 10, "extrusor": 2}])
    res = cpm.laminar(CP, r["archivo"], MAQUINA, PROCESO, [FILAMENTO, NEGRO],
                      ajustes={"enable_prime_tower": 0}, modelo_esperado="Creality K2", cama=(260, 260, 260))
    g = res.gcodes[0]
    assert g["apto"], g["problemas"]
    cambios = [(z, t) for z, t in herramientas_por_altura(g["archivo"]) if t == "T1"]
    assert len(cambios) == 1 and 9.5 < cambios[0][0] < 10.5


@instalado
def test_objeto_en_una_mezcla_alterna_capas(tmp_path):
    r = cpm.preparar_multicolor(cubo_stl(tmp_path / "cubo.stl"), asignar={"cubo": 3})
    res = cpm.laminar(CP, r["archivo"], MAQUINA, PROCESO, [FILAMENTO, NEGRO], mezclas=[{"a": 1, "b": 2}],
                      ajustes={"enable_prime_tower": 0}, modelo_esperado="Creality K2", cama=(260, 260, 260))
    herramientas = [t for _, t in herramientas_por_altura(res.gcodes[0]["archivo"])]
    assert herramientas.count("T0") > 20 and herramientas.count("T1") > 20
    assert any("mezclas" in a for a in res.ajustes_aplicados)
