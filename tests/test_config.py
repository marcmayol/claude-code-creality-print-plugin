"""Configuración personal: impresora guardada y notas, siempre fuera del repo."""
import pytest

import config
import impresora as imp


@pytest.fixture(autouse=True)
def config_temporal(tmp_path, monkeypatch):
    monkeypatch.setenv("CREALITY_PLUGIN_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.delenv("K2_HOST", raising=False)
    monkeypatch.delenv("CREALITY_HOST", raising=False)


def test_sin_configuracion_no_falla():
    assert config.leer() == {}
    assert config.impresora_guardada() is None


def test_guardar_impresora():
    config.guardar_impresora("10.0.0.9", "F021", "mi-k2")
    assert config.impresora_guardada() == ("10.0.0.9", "F021")


def test_impresora_guardada_manda_sobre_creality_print(monkeypatch):
    config.guardar_impresora("10.0.0.9", "F021", "mi-k2")
    probados = []

    def falso_get(url, timeout):
        probados.append(url)
        raise imp.httpx.ConnectError("no")

    monkeypatch.setattr(imp, "_hosts_de_creality_print", lambda: [("10.0.0.5", "F021")])
    monkeypatch.setattr(imp.httpx, "get", falso_get)
    with pytest.raises(imp.ErrorImpresora, match="configurar_impresora"):
        imp.Impresora.detectar()
    assert "10.0.0.9" in probados[0] and "10.0.0.5" in probados[1]


def test_k2_host_antiguo_sigue_valiendo(monkeypatch):
    config.guardar_impresora("10.0.0.9", "F021")
    monkeypatch.setenv("K2_HOST", "10.0.0.7")
    assert imp.Impresora.detectar().host == "10.0.0.7"


def test_creality_host_manda_sobre_todo(monkeypatch):
    config.guardar_impresora("10.0.0.9", "F021")
    monkeypatch.setenv("K2_HOST", "10.0.0.7")
    monkeypatch.setenv("CREALITY_HOST", "10.0.0.8")
    assert imp.Impresora.detectar().host == "10.0.0.8"
