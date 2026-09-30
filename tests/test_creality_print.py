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
    with pytest.raises(cpm.ErrorCrealityPrint, match="Did you mean"):
        CP.resolver_perfil("process", "0.20mm Standard @Creality K2 0.4 nozle")


@instalado
def test_listar_no_mezcla_k2_pro():
    nombres = [p["name"] for p in CP.listar_perfiles("process", "0.20mm", MAQUINA)]
    assert PROCESO in nombres
    assert not any("K2 Pro" in n or "K2 Plus" in n for n in nombres)


@instalado
def test_laminar_stl_con_ajustes(tmp_path):
    stl = cubo_stl(tmp_path / "cubo.stl")
    res = cpm.laminar(CP, stl, MAQUINA, PROCESO, [FILAMENTO], ajustes={"wall_loops": 3},
                      modelo_esperado="Creality K2", cama=(260, 260, 260))
    assert len(res.gcodes) == 1
    g = res.gcodes[0]
    assert g["ok_to_print"], g["problems"]
    assert g["printer_model"] == "Creality K2"
    assert g["thumbnails"] == ["96x96", "300x300"]
    assert "cubo.stl_PLA_" in g["file"]
    inf = gcode.analizar(g["file"])
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
    assert g["ok_to_print"], g["problems"]
    assert [f["extruder"] for f in g["filaments"]] == [1, 2]
    assert any("centred" in a for a in res.avisos)


def test_meter_perfiles_en_3mf(tmp_path):
    ruta = _3mf_plano(tmp_path / "a.3mf")
    with zipfile.ZipFile(ruta, "a") as z:
        z.writestr("Metadata/project_settings.config",
                   '{"printer_settings_id": "Creality K2 Plus 0.4 nozzle", "filament_colour": ["#FF0000", "#00FF00"],'
                   ' "clave_del_proyecto": "se queda"}')
    planos = {"machine": [{"name": "M", "inherits": "", "printable_area": ["0x0", "260x0"]}],
              "process": [{"name": "P", "wall_loops": "3"}],
              "filament": [{"name": "F1", "nozzle_temperature": ["220"], "filament_colour": ["#FF0000"]},
                           {"name": "F2", "nozzle_temperature": ["230"], "filament_colour": ["#00FF00"]}]}
    cpm.meter_perfiles_en_3mf(ruta, tmp_path / "b.3mf", planos)
    with zipfile.ZipFile(tmp_path / "b.3mf") as z:
        ajustes = __import__("json").loads(z.read("Metadata/project_settings.config"))
        assert "3D/3dmodel.model" in z.namelist()
    assert ajustes["printer_settings_id"] == "M" and ajustes["print_settings_id"] == "P"
    assert ajustes["filament_settings_id"] == ["F1", "F2"]
    assert ajustes["printable_area"] == ["0x0", "260x0"] and ajustes["wall_loops"] == "3"
    assert ajustes["nozzle_temperature"] == ["220", "230"]
    assert ajustes["filament_colour"] == ["#FF0000", "#00FF00"]
    assert ajustes["clave_del_proyecto"] == "se queda"
    assert "inherits" not in ajustes


def _proyecto_creality(ruta, lado=20.0):
    """Un proyecto como los que guarda Creality Print: objeto con componentes, model_settings y placa."""
    h = lado / 2
    v = [(x, y, z) for x in (-h, h) for y in (-h, h) for z in (-h, h)]
    caras = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1),
             (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]
    ns = ('xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
          'xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
          'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" requiredextensions="p"')
    malla = ("".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in v),
             "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in caras))
    with zipfile.ZipFile(ruta, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        z.writestr("3D/_rels/3dmodel.model.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Target="/3D/Objects/object_1.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        z.writestr("3D/Objects/object_1.model", f'<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" {ns}>'
                   '<metadata name="BambuStudio:3mfVersion">1</metadata><resources><object id="1" type="model">'
                   f'<mesh><vertices>{malla[0]}</vertices><triangles>{malla[1]}</triangles></mesh></object></resources><build/></model>')
        z.writestr("3D/3dmodel.model", f'<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" {ns}>'
                   '<metadata name="Application">Creality_Print V7.3.0.6151 Release</metadata>'
                   '<metadata name="BambuStudio:3mfVersion">1</metadata><resources><object id="2" type="model"><components>'
                   '<component p:path="/3D/Objects/object_1.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
                   f'</components></object></resources><build><item objectid="2" transform="1 0 0 0 1 0 0 0 1 175 175 {h}" printable="1"/></build></model>')
        z.writestr("Metadata/model_settings.config", '<?xml version="1.0" encoding="UTF-8"?><config><object id="2">'
                   '<metadata key="name" value="cubo"/><metadata key="extruder" value="1"/>'
                   '<part id="1" subtype="normal_part"><metadata key="name" value="cubo"/></part></object>'
                   '<plate><metadata key="plater_id" value="1"/><model_instance><metadata key="object_id" value="2"/>'
                   '<metadata key="instance_id" value="0"/></model_instance></plate></config>')
    return ruta


@instalado
def test_laminar_proyecto_de_otra_maquina_impone_la_k2(tmp_path):
    # Un 3MF de proyecto trae la máquina de quien lo subió (aquí una K2 Plus, cama de 350).
    # La 7.3 no admite --load-settings con un proyecto, así que la K2 tiene que ir dentro.
    plus = {"machine": [CP.resolver_perfil("machine", "Creality K2 Plus 0.4 nozzle")],
            "process": [CP.resolver_perfil("process", "0.20mm Standard @Creality K2 Plus 0.4 nozzle")],
            "filament": [CP.resolver_perfil("filament", "Hyper PLA @Creality K2 Plus 0.4 nozzle")]}
    ruta = tmp_path / "proyecto.3mf"
    cpm.meter_perfiles_en_3mf(_proyecto_creality(tmp_path / "base.3mf"), ruta, plus)
    res = cpm.laminar(CP, ruta, MAQUINA, PROCESO, [FILAMENTO], ajustes={"wall_loops": 4},
                      modelo_esperado="Creality K2", cama=(260, 260, 260))
    g = res.gcodes[0]
    assert g["ok_to_print"], g["problems"]
    assert g["printer_model"] == "Creality K2"
    assert g["machine_profile"] == MAQUINA
    assert gcode.analizar(g["file"]).valor("wall_loops") == "4"


@instalado
def test_error_de_laminado_no_deja_basura(tmp_path):
    malo = tmp_path / "roto.stl"
    malo.write_text("esto no es un stl")
    with pytest.raises(cpm.ErrorCrealityPrint):
        cpm.laminar(CP, malo, MAQUINA, PROCESO, [FILAMENTO])
    assert not list(tmp_path.glob("cp_laminar_*"))
