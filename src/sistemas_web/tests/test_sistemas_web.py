"""Pruebas de la consola del mini sistema legado (`src.sistemas_web`).

Es otra app, con su propio `TestClient`. Los casos se siembran por `servicio`,
como lo haria Care AI desde su proceso: la consola solo ve lo que quedo escrito
en `sistemas.db`.
"""

import pytest
from fastapi.testclient import TestClient

from src.agente import servicio
from src.sistemas_web import sistemas
from src.sistemas_web import app as consola
from src.agente.tests.test_fase2 import _aprobado


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


def test_restaurar_puede_vaciar_tambien_la_bandeja(cliente, monkeypatch):
    """La consola le pide a la bandeja por HTTP que archive; aqui ese HTTP va a la
    app de Care AI de verdad, por su TestClient."""
    from src.web import app as app_bandeja

    _aprobado("c3")
    with TestClient(app_bandeja) as care:
        monkeypatch.setattr(consola, "_pedir_vaciado",
                            lambda: care.post("/demo/vaciar").json()["casos_archivados"])
        html = cliente.post("/restaurar", data={"cuenta": "B2B-005130", "confirmo": "1",
                                                "vaciar_bandeja": "1"}).text
    assert "1 caso(s) archivado(s)" in html
    assert servicio.resumen()["resueltos"] == 0
    assert sistemas.servicio("SVC-9310")["ancho_banda_mbps"] == 200


def test_si_la_bandeja_no_contesta_los_sistemas_se_restauran_igual(cliente, monkeypatch):
    def caida():
        raise ConnectionRefusedError
    monkeypatch.setattr(consola, "_pedir_vaciado", caida)
    _aprobado("c4")
    html = cliente.post("/restaurar", data={"cuenta": "B2B-005130", "confirmo": "1",
                                            "vaciar_bandeja": "1"}).text
    assert "No se pudo vaciar la bandeja" in html
    assert sistemas.servicio("SVC-9310")["ancho_banda_mbps"] == 200


# --------------------------------------------------------------------------- #
# Clientes de prueba
# --------------------------------------------------------------------------- #
def _form_cliente(correo="yo.pruebas@gmail.com", **extra) -> dict:
    """Como lo manda el navegador: columnas paralelas, con una fila en blanco al final."""
    return {
        "razon_social": "Transportes Prueba", "segmento": "Pyme", "estado": "activo", "dominio": "",
        "contacto_correo": [correo, ""], "contacto_nombre": ["Yo", ""], "contacto_autorizado": ["1", "1"],
        "servicio_sede": ["Bogota - Norte", ""], "servicio_producto": ["Internet Dedicado"] * 2,
        "servicio_mbps": ["200", "200"], "servicio_nodo": ["BOG-N21", "BOG-N21"],
        "con_contrato": "1", "contrato_vigencia": "2027-12-31", "contrato_valor": "5000000",
        "contrato_upgrade": "1", "contrato_permanencia": "12",
        "factura_periodo": ["2026-09"], "factura_valor": ["5800000"], "factura_estado": ["pendiente"],
        **extra,
    }


def test_crear_cliente_lo_deja_listo_para_el_copiloto(cliente):
    r = cliente.post("/clientes", data=_form_cliente(), follow_redirects=False)
    assert r.status_code == 303
    cuenta_id = r.headers["location"].split("cuenta=")[1].split("&")[0]

    cuenta, autorizado = sistemas.cuenta_por_contacto("yo.pruebas@gmail.com")
    assert cuenta["cuenta_id"] == cuenta_id and autorizado
    assert [s["sede"] for s in sistemas.servicios(cuenta_id)] == ["Bogota - Norte"]    # la fila vacia no entra
    assert sistemas.contrato(cuenta_id)["valor_mensual_pactado"] == 5_000_000
    assert sistemas.facturas(cuenta_id)[0]["valor"] == 5_800_000
    # Sin dominio corporativo: otro remitente de gmail.com no cae en esta cuenta.
    assert sistemas.cuenta_por_dominio("gmail.com") is None

    html = cliente.get(r.headers["location"]).text
    assert "Transportes Prueba" in html and "cliente de prueba" in html


def test_crear_cliente_con_error_no_deja_nada_y_conserva_lo_escrito(cliente):
    r = cliente.post("/clientes", data=_form_cliente(correo="ltrigosg@ufpso.edu.co"))
    assert r.status_code == 400
    assert "ya es contacto de la cuenta B2B-005130" in r.text
    assert 'value="Transportes Prueba"' in r.text
    assert len(sistemas.cuentas()) == 1                                  # todo o nada


def test_los_clientes_de_prueba_sobreviven_a_restaurar(cliente):
    cuenta_id = sistemas.crear_cuenta("Otra Prueba", contactos=[{"correo": "otra@gmail.com"}],
                                      servicios=[{"sede": "Ocana", "ancho_banda_mbps": 100, "nodo": "OCA-N02"}])
    sistemas.agregar_contacto("B2B-005130", "mio@gmail.com")
    sid = sistemas.servicios(cuenta_id)[0]["servicio_id"]
    sistemas.aplicar_ancho_banda(sid, 300)

    cliente.post("/restaurar", data={"cuenta": cuenta_id, "confirmo": "1", "conservar": "1"})
    assert sistemas.servicio(sid)["ancho_banda_mbps"] == 100             # vuelve a su estado inicial
    assert sistemas.cuenta_por_contacto("mio@gmail.com")[0]["cuenta_id"] == "B2B-005130"

    cliente.post("/restaurar", data={"cuenta": cuenta_id, "confirmo": "1"})
    assert [c["cuenta_id"] for c in sistemas.cuentas()] == ["B2B-005130"]


def test_solo_se_elimina_lo_creado_en_la_consola(cliente):
    cuenta_id = sistemas.crear_cuenta("Borrable", contactos=[{"correo": "borrar@gmail.com"}])
    r = cliente.post("/clientes/B2B-005130/eliminar", data={"confirmo": "1"}, follow_redirects=False)
    assert "error=" in r.headers["location"]
    r = cliente.post("/clientes/B2B-005130/contactos/eliminar", data={"correo": "ltrigosg@ufpso.edu.co"},
                     follow_redirects=False)
    assert "error=" in r.headers["location"]

    cliente.post(f"/clientes/{cuenta_id}/eliminar", data={"confirmo": "1"})
    assert sistemas.cuenta_por_contacto("borrar@gmail.com") == (None, False)


def test_el_sistema_rechaza_altas_invalidas():
    with pytest.raises(sistemas.ErrorSistema, match="nodo"):
        sistemas.agregar_servicio("B2B-005130", "Sede", 100, "NO-EXISTE")
    with pytest.raises(sistemas.ErrorSistema, match="AAAA-MM"):
        sistemas.agregar_factura("B2B-005130", "septiembre", 1000)
    with pytest.raises(sistemas.ErrorSistema, match="ya tiene contrato"):
        sistemas.agregar_contrato("B2B-005130", "2027-01-01", 1000)
    with pytest.raises(sistemas.ErrorSistema, match="dominio"):
        sistemas.crear_cuenta("Dup", dominio="ufpso.edu.co", contactos=[{"correo": "x@y.com"}])


def test_editar_la_cuenta_cambia_la_ficha_y_deja_rastro(cliente):
    r = cliente.post("/clientes/B2B-005130/editar", data={
        "razon_social": "UFPSO Ocana", "segmento": "Gobierno", "estado": "suspendido", "dominio": ""},
        follow_redirects=False)
    assert "ok=" in r.headers["location"]
    c = sistemas.vista_cuenta("B2B-005130")["cuenta"]
    assert (c["razon_social"], c["segmento"], c["estado"]) == ("UFPSO Ocana", "Gobierno", "suspendido")
    assert sistemas.dominio_visible(c["dominio"]) == ""                   # sin dominio corporativo
    assert sistemas.cuenta_por_dominio("ufpso.edu.co") is None
    assert [o["operacion"] for o in sistemas.operaciones("B2B-005130")] == ["editar_cuenta"]
    assert "UFPSO Ocana" in cliente.get(r.headers["location"]).text

    sistemas.restaurar()                                                 # la semilla vuelve a su estado
    assert sistemas.vista_cuenta("B2B-005130")["cuenta"]["razon_social"] == "UFPSO (cuenta de pruebas)"


def test_editar_un_contacto_cambia_como_se_reconoce_al_cliente(cliente):
    r = cliente.post("/clientes/B2B-005130/contactos/editar", data={
        "correo_actual": "ltrigosg@ufpso.edu.co", "correo": "otro@gmail.com", "nombre": "Leo", "autorizado": "0"},
        follow_redirects=False)
    assert "ok=" in r.headers["location"]
    assert sistemas.cuenta_por_contacto("otro@gmail.com")[0]["cuenta_id"] == "B2B-005130"
    assert sistemas.cuenta_por_contacto("otro@gmail.com")[1] is False
    assert sistemas.cuenta_por_contacto("ltrigosg@ufpso.edu.co") == (None, False)


def test_un_contacto_editado_en_la_consola_sobrevive_a_restaurar():
    sistemas.agregar_contacto("B2B-005130", "mio@gmail.com", "Yo")
    sistemas.editar_contacto("B2B-005130", "mio@gmail.com", correo="mio2@gmail.com", nombre="Yo mismo")
    sistemas.restaurar()
    assert sistemas.cuenta_por_contacto("mio2@gmail.com")[0]["cuenta_id"] == "B2B-005130"
    assert sistemas.cuenta_por_contacto("mio@gmail.com") == (None, False)


def test_el_sistema_rechaza_ediciones_invalidas():
    sistemas.crear_cuenta("Otra", dominio="otra.com", contactos=[{"correo": "x@otra.com"}])
    with pytest.raises(sistemas.ErrorSistema, match="obligatoria"):
        sistemas.editar_cuenta("B2B-005130", razon_social=" ", segmento="Pyme", estado="activo")
    with pytest.raises(sistemas.ErrorSistema, match="ya es de la cuenta"):
        sistemas.editar_cuenta("B2B-005130", razon_social="U", segmento="Pyme", estado="activo", dominio="otra.com")
    with pytest.raises(sistemas.ErrorSistema, match="ya es contacto"):
        sistemas.editar_contacto("B2B-005130", "ltrigosg@ufpso.edu.co", correo="x@otra.com")
    with pytest.raises(sistemas.ErrorSistema, match="no es contacto de la cuenta"):
        sistemas.editar_contacto("B2B-005130", "x@otra.com", correo="y@otra.com")


def test_la_consola_no_depende_de_care_ai():
    """La separacion es de codigo, no solo de pantalla: la consola no importa la
    bandeja ni el grafo."""
    fuente = open(consola.__file__, encoding="utf-8").read()
    for modulo in ("db", "servicio", "graph", "nodes", "backends"):
        assert f"import {modulo}" not in fuente and f", {modulo}" not in fuente
