"""Traducciones al protocolo de Creality y, si la K2 está en la red, lecturas reales.

Aquí no hay NADA que mueva la impresora: ni subir, ni imprimir, ni pausar.
"""
import pytest

import impresora as imp


def test_id_extrusor_como_creality_print():
    assert [imp.id_extrusor(e) for e in (1, 2, 4, 5, 16)] == ["T1A", "T1B", "T1D", "T2A", "T4D"]


@pytest.mark.parametrize("texto,esperado", [("1A", (1, 0)), ("1d", (1, 3)), ("T2C", (2, 2)), ("b", (1, 1))])
def test_ranuras(texto, esperado):
    assert imp.ranura_a_indices(texto) == esperado


@pytest.mark.parametrize("malo", ["1E", "A1", "", "12"])
def test_ranuras_malas(malo):
    with pytest.raises(imp.ErrorImpresora):
        imp.ranura_a_indices(malo)


def test_mapeo_legible():
    assert imp._mapeo_legible({"id": "T1A", "boxId": 1, "materialId": 3}) == {"extrusor": 1, "ranura": "1D"}
    assert imp._mapeo_legible({"id": "T2B", "boxId": 2, "materialId": 0}) == {"extrusor": 6, "ranura": "2A"}


def test_modelos_incluyen_familia_k2_y_k1():
    assert imp.MODELOS["F021"] == ("Creality K2", (260, 260, 260))
    assert imp.MODELOS["F012"][1] == (300, 300, 300)
    assert imp.MODELOS["CR-K1"][0] == "Creality K1"
    assert imp.MODELOS["K1C"][1][:2] == (220, 220)


def test_color_de_creality():
    assert imp._color("#0ffffff") == "#FFFFFF"
    assert imp._color("#0ba552a") == "#BA552A"
    assert imp._color(None) is None


try:
    K2 = imp.Impresora.detectar()
except imp.ErrorImpresora:
    K2 = None
en_red = pytest.mark.skipif(K2 is None, reason="la K2 no está en la red")


@en_red
def test_estado_real():
    e = K2.estado()
    assert e["klipper"] in ("ready", "startup", "shutdown", "error")
    assert e["estado"] in ("standby", "printing", "paused", "complete", "cancelled", "error")


@en_red
def test_cfs_real():
    c = K2.cfs()
    assert [r["ranura"] for r in c["ranuras"]][:4] == ["1A", "1B", "1C", "1D"]


@en_red
def test_archivos_real():
    assert isinstance(K2.archivos(limite=2), list)


@en_red
def test_dir_gcodes_lo_dice_moonraker():
    assert K2.dir_gcodes.endswith("/printer_data/gcodes")
