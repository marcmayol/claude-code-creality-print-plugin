"""Perfiles, ajustes, 3MF y laminado real con la CLI de Creality Print (si está instalado)."""
import zipfile

import pytest

import creality_print as cpm
import gcode

try:
    CP = cpm.CrealityPrint.detectar()
except cpm.ErrorCrealityPrint:
    CP = None
instalado = pytest.mark.skipif(CP is None, reason="Creality Print no está instalado")

MAQUINA = "Creality K2 0.4 nozzle"
PROCESO = "0.20mm Standard @Creality K2 0.4 nozzle"
FILAMENTO = "Hyper PLA @Creality K2 0.4 nozzle"


def cubo_stl(ruta, lado=20.0):
    v = [(x, y, z) for x in (0, lado) for y in (0, lado) for z in (0, lado)]
    caras = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1),
             (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]
    partes = ["solid cubo"]
    for a, b, c in caras:
        partes.append("facet normal 0 0 0\nouter loop")
        partes += [f"vertex {v[i][0]} {v[i][1]} {v[i][2]}" for i in (a, b, c)]
        partes.append("endloop\nendfacet")
    partes.append("endsolid cubo")
    ruta.write_text("\n".join(partes) + "\n")
    return ruta


def test_aplicar_ajustes_va_al_perfil_que_la_tiene():
    perfiles = {"machine": [{"printable_height": "260"}],
                "process": [{"wall_loops": "2", "sparse_infill_density": "15%"}],
                "filament": [{"hot_plate_temp": ["55"]}, {"hot_plate_temp": ["60"]}]}
    hechos = cpm.aplicar_ajustes(perfiles, {"wall_loops": 4, "hot_plate_temp": 50})
    assert perfiles["process"][0]["wall_loops"] == "4"
    assert perfiles["filament"][0]["hot_plate_temp"] == ["50"]
    assert perfiles["filament"][1]["hot_plate_temp"] == ["50"]
    assert len(hechos) == 2


def test_ajuste_inexistente_da_error_con_pista():
    perfiles = {"machine": [{}], "process": [{"wall_loops": "2"}], "filament": [{}]}
    with pytest.raises(cpm.ErrorCrealityPrint, match="wall_loops"):
        cpm.aplicar_ajustes(perfiles, {"wall_loop": 3})


def test_booleanos_como_0_1():
    perfiles = {"machine": [{}], "process": [{"enable_support": "0"}], "filament": [{}]}
    cpm.aplicar_ajustes(perfiles, {"enable_support": True})
    assert perfiles["process"][0]["enable_support"] == "1"


TETRA = ('<mesh><vertices>{v}</vertices><triangles><triangle v1="0" v2="1" v3="2"/>'
         '<triangle v1="0" v2="3" v3="1"/><triangle v1="1" v2="3" v3="2"/><triangle v1="2" v2="3" v3="0"/>'
         '</triangles></mesh>')


def _vertices(puntos):
    return "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in puntos)


def _3mf_plano(ruta):
    base = TETRA.format(v=_vertices([(-50, -10, 0), (50, -10, 0), (0, 10, 0), (0, 0, 5)]))
    texto = TETRA.format(v=_vertices([(-5, -5, 5), (5, -5, 5), (0, 5, 5), (0, 0, 6)]))
    modelo = (
        '<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><resources>'
        '<basematerials id="1"><base name="Blanco" displaycolor="#FFFFFFFF"/>'
        '<base name="Negro" displaycolor="#111111FF"/></basematerials>'
        f'<object id="2" name="Base" type="model" pid="1" pindex="0">{base}</object>'
        f'<object id="3" name="Texto" type="model" pid="1" pindex="1">{texto}</object>'
        '</resources><build><item objectid="2"/><item objectid="3"/></build></model>'
    )
    with zipfile.ZipFile(ruta, "w") as z:
        z.writestr("3D/3dmodel.model", modelo)
        z.writestr("[Content_Types].xml", "<Types/>")
    return ruta


def test_preparar_3mf_plano_centra_y_asigna_extrusores(tmp_path):
    info = cpm.preparar_3mf_plano(_3mf_plano(tmp_path / "a.3mf"), tmp_path / "b.3mf", (260, 260))
    assert info["centrado"]
    assert info["desplazamiento_mm"] == [130.0, 130.0, 0.0]
    assert info["num_filamentos"] == 2
    assert info["colores"] == ["#FFFFFF", "#111111"]
    with zipfile.ZipFile(tmp_path / "b.3mf") as z:
        config = z.read("Metadata/model_settings.config").decode()
        modelo = z.read("3D/3dmodel.model").decode()
    bloques = config.split("<object ")
    assert 'id="2"' in bloques[1] and 'key="extruder" value="1"' in bloques[1]
    assert 'id="3"' in bloques[2] and 'key="extruder" value="2"' in bloques[2]
    assert 'transform="1 0 0 0 1 0 0 0 1 130.0000 130.0000 0.0000"' in modelo


def test_proyecto_de_creality_no_se_toca(tmp_path):
    ruta = _3mf_plano(tmp_path / "a.3mf")
    with zipfile.ZipFile(ruta, "a") as z:
        z.writestr("Metadata/model_settings.config", "<config/>")
    assert cpm.preparar_3mf_plano(ruta, tmp_path / "b.3mf", (260, 260)) is None


# ------------------------------------------------------------ con Creality Print de verdad

@instalado
def test_resolver_perfil_hereda_todo():
    plano = CP.resolver_perfil("filament", FILAMENTO)
    assert plano["inherits"] == ""
    assert "nozzle_temperature" in plano
    assert plano["name"] == FILAMENTO


@instalado
def test_perfil_inexistente_sugiere():
    with pytest.raises(cpm.ErrorCrealityPrint, match="Querías"):
        CP.resolver_perfil("process", "0.20mm Standard @Creality K2 0.4 nozle")


@instalado
def test_listar_no_mezcla_k2_pro():
    nombres = [p["nombre"] for p in CP.listar_perfiles("process", "0.20mm", MAQUINA)]
    assert PROCESO in nombres
    assert not any("K2 Pro" in n or "K2 Plus" in n for n in nombres)


@instalado
def test_laminar_stl_con_ajustes(tmp_path):
    stl = cubo_stl(tmp_path / "cubo.stl")
    res = cpm.laminar(CP, stl, MAQUINA, PROCESO, [FILAMENTO], ajustes={"wall_loops": 3},
                      modelo_esperado="Creality K2", cama=(260, 260, 260))
    assert len(res.gcodes) == 1
    g = res.gcodes[0]
    assert g["apto"], g["problemas"]
    assert g["impresora"] == "Creality K2"
    assert g["miniaturas"] == ["96x96", "300x300"]
    assert "cubo.stl_PLA_" in g["archivo"]
    inf = gcode.analizar(g["archivo"])
    assert inf.valor("wall_loops") == "3"
    assert abs(inf.pieza.z_max - 20) < 0.5
    assert 115 < inf.pieza.x_min < inf.pieza.x_max < 145  # centrado en la cama


@instalado
def test_laminar_rechaza_perfil_de_otra_maquina(tmp_path):
    stl = cubo_stl(tmp_path / "cubo.stl")
    with pytest.raises(cpm.ErrorCrealityPrint, match="K2 Pro"):
        cpm.laminar(CP, stl, "Creality K2 Pro 0.4 nozzle", "0.20mm Standard @Creality K2 Pro 0.4 nozzle",
                    ["Hyper PLA @Creality K2 Pro 0.4 nozzle"], modelo_esperado="Creality K2")


@instalado
def test_laminar_3mf_plano_bicolor(tmp_path):
    ruta = _3mf_plano(tmp_path / "bicolor.3mf")
    res = cpm.laminar(CP, ruta, MAQUINA, PROCESO, [FILAMENTO, "CR-PLA @Creality K2 0.4 nozzle"],
                      modelo_esperado="Creality K2", cama=(260, 260, 260))
    g = res.gcodes[0]
    assert g["apto"], g["problemas"]
    assert [f["extrusor"] for f in g["filamentos"]] == [1, 2]
    assert any("recolocado" in a for a in res.avisos)


@instalado
def test_error_de_laminado_no_deja_basura(tmp_path):
    malo = tmp_path / "roto.stl"
    malo.write_text("esto no es un stl")
    with pytest.raises(cpm.ErrorCrealityPrint):
        cpm.laminar(CP, malo, MAQUINA, PROCESO, [FILAMENTO])
    assert not list(tmp_path.glob("cp_laminar_*"))
