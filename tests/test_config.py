"""Configuración personal: impresora guardada y notas, siempre fuera del repo."""
import pytest

import config
import k2


@pytest.fixture(autouse=True)
def config_temporal(tmp_path, monkeypatch):
    monkeypatch.setenv("CREALITY_PLUGIN_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.delenv("K2_HOST", raising=False)


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
        raise k2.httpx.ConnectError("no")

    monkeypatch.setattr(k2, "_hosts_de_creality_print", lambda: [("10.0.0.5", "F021")])
    monkeypatch.setattr(k2.httpx, "get", falso_get)
    with pytest.raises(k2.ErrorImpresora, match="configurar_impresora"):
        k2.K2.detectar()
    assert "10.0.0.9" in probados[0] and "10.0.0.5" in probados[1]


def test_k2_host_manda_sobre_todo(monkeypatch):
    config.guardar_impresora("10.0.0.9", "F021")
    monkeypatch.setenv("K2_HOST", "10.0.0.7")
    assert k2.K2.detectar().host == "10.0.0.7"
