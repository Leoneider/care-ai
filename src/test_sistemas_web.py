"""Pruebas de la consola del mini sistema legado (`src.sistemas_web`).

Es otra app, con su propio `TestClient`. Los casos se siembran por `servicio`,
como lo haria Care AI desde su proceso: la consola solo ve lo que quedo escrito
en `sistemas.db`.
"""

import pytest
from fastapi.testclient import TestClient

from src import servicio, sistemas
from src.sistemas_web import app as consola
from src.test_fase2 import _aprobado


@pytest.fixture
def cliente():
    with TestClient(consola.app) as c:
        yield c


def test_abre_en_la_cuenta_de_la_demo(cliente):
    html = cliente.get("/").text
    assert "UFPSO (cuenta de pruebas)" in html
    assert "Bogota - Sede Chapinero" in html
    assert "7.150.000" in html                     # la factura de septiembre, antes del caso


def test_tras_el_caso_muestra_lo_que_cambio(cliente):
    _aprobado("c1")
    html = cliente.get("/?cuenta=B2B-005130").text
    assert "500 Mbps" in html
    assert "NC-70001" in html
    assert "aplicar_ancho_banda" in html and "emitir_nota_credito" in html
    assert 'class="tocado"' in html


def test_una_cuenta_desconocida_cae_en_la_de_la_demo(cliente):
    assert "UFPSO (cuenta de pruebas)" in cliente.get("/?cuenta=NO-EXISTE").text


def test_restaurar_pide_confirmacion_y_no_toca_la_bandeja(cliente):
    _aprobado("c2")
    cliente.post("/restaurar", data={"cuenta": "B2B-005130"})
    assert sistemas.servicio("SVC-9310")["ancho_banda_mbps"] == 500     # sin casilla, nada

    html = cliente.post("/restaurar", data={"cuenta": "B2B-005130", "confirmo": "1"}).text
    assert "Sistemas restaurados" in html
    assert sistemas.servicio("SVC-9310")["ancho_banda_mbps"] == 200
    assert sistemas.operaciones("B2B-005130") == []
    assert servicio.resumen()["resueltos"] == 1                       # el caso sigue en Care AI


def test_la_consola_no_depende_de_care_ai():
    """La separacion es de codigo, no solo de pantalla: la consola no importa la
    bandeja ni el grafo."""
    fuente = open(consola.__file__, encoding="utf-8").read()
    for modulo in ("db", "servicio", "graph", "nodes", "backends"):
        assert f"import {modulo}" not in fuente and f", {modulo}" not in fuente
