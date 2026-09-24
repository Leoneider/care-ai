"""Pruebas de la UI del ejecutivo, contra la app real con TestClient.

El foco esta en la traduccion formulario -> decision, que es donde la UI puede
mentirle al dataset: aprobar guardando algo distinto de lo propuesto, o mandar
acciones finales cuando el ejecutivo no toco nada.
"""

import re

import pytest
from fastapi.testclient import TestClient

from src import db, servicio
from src.demo import CORREO
from src.web.app import app, bloqueantes_de


@pytest.fixture
def cliente():
    with TestClient(app) as c:
        yield c


def _caso_nuevo(sufijo: str) -> str:
    res = servicio.procesar_correo(dict(CORREO, message_id=f"<w-{sufijo}@mail.acme.com.co>"))
    assert res["estado"] == "pendiente"
    return res["thread_id"]


def _campos(thread_id: str) -> dict:
    """El formulario tal como lo enviaria el navegador, sin tocar nada."""
    caso = servicio.detalle(thread_id)["caso"]
    datos: dict = {"sel": [], "comentario": ""}
    for s in caso["solicitudes"]:
        for i, a in enumerate(s["acciones"]):
            datos["sel"].append(f"{s['id']}::{i}")
            datos[f"desc::{s['id']}::{i}"] = a["descripcion"]
            for k, v in (a["parametros"] or {}).items():
                datos[f"par::{s['id']}::{i}::{k}"] = v
    return datos


def test_bandeja_vacia(cliente):
    html = cliente.get("/").text
    assert "No hay casos esperando revision" in html


def test_bandeja_lista_el_caso_pendiente(cliente):
    _caso_nuevo("a")
    html = cliente.get("/").text
    assert "ACME Logistica S.A.S." in html
    assert "Ampliacion sede Bogota" in html


def test_detalle_muestra_fuente_validaciones_y_acciones(cliente):
    """Lo que hace util la tarjeta: el fragmento literal que sustenta cada
    solicitud y el resultado de los chequeos deterministas."""
    html = cliente.get(f"/caso/{_caso_nuevo('b')}").text
    assert "la factura de septiembre llego por un valor distinto al pactado" in html
    assert "factura_vs_contrato" in html
    assert "APLICAR_UPGRADE_ANCHO_BANDA" in html
    assert "Aprobar" in html


def test_aprobar_sin_tocar_nada_se_registra_como_aprobar(cliente):
    thread = _caso_nuevo("c")
    cliente.post(f"/caso/{thread}/decidir", data={**_campos(thread), "decision": "aprobar"})

    assert [c["decision"] for c in servicio.listar(db.RESUELTO)] == ["aprobar"]
    assert servicio.detalle(thread)["resultado"]["estado"] == "ejecutado"


def test_desmarcar_una_accion_convierte_el_aprobar_en_editar(cliente):
    """El nucleo de la UI: un boton que dice aprobar pero guarda algo distinto de
    lo propuesto arruina la metrica de cuantos casos pasan sin intervencion."""
    thread = _caso_nuevo("d")
    datos = _campos(thread)
    total = len(datos["sel"])
    datos["sel"] = datos["sel"][:-1]

    cliente.post(f"/caso/{thread}/decidir", data={**datos, "decision": "aprobar"})

    assert [c["decision"] for c in servicio.listar(db.RESUELTO)] == ["editar"]
    assert len(servicio.detalle(thread)["resultado"]["acciones"]) == total - 1


def test_editar_un_parametro_tambien_cuenta_como_edicion(cliente):
    thread = _caso_nuevo("e")
    datos = _campos(thread)
    clave = next(k for k in datos if k.startswith("par::"))
    datos[clave] = "VALOR-CORREGIDO"

    cliente.post(f"/caso/{thread}/decidir", data={**datos, "decision": "aprobar"})

    final = servicio.detalle(thread)["decision_humana"]
    assert final["decision"] == "editar"
    assert any("VALOR-CORREGIDO" in (a["parametros"] or {}).values() for a in final["acciones_finales"])


def test_rechazar_descarta_el_caso_con_su_motivo(cliente):
    thread = _caso_nuevo("f")
    cliente.post(f"/caso/{thread}/decidir", data={"decision": "rechazar", "comentario": "es spam"})

    assert servicio.detalle(thread)["resultado"] == {"estado": "descartado", "motivo": "es spam"}


def test_caso_resuelto_se_abre_en_modo_lectura(cliente):
    thread = _caso_nuevo("g")
    cliente.post(f"/caso/{thread}/decidir", data={**_campos(thread), "decision": "aprobar"})

    html = cliente.get(f"/caso/{thread}").text
    assert "Caso ya resuelto" in html
    assert "APLICAR_UPGRADE_ANCHO_BANDA" in html      # sigue mostrando lo que se decidio
    assert 'name="decision" value="aprobar"' not in html   # sin botones


def test_segundo_submit_no_cambia_la_decision(cliente):
    """Doble click en Aprobar, o el boton Atras del navegador."""
    thread = _caso_nuevo("h")
    datos = _campos(thread)
    cliente.post(f"/caso/{thread}/decidir", data={**datos, "decision": "aprobar"})
    cliente.post(f"/caso/{thread}/decidir", data={"decision": "rechazar", "comentario": "ups"})

    assert [c["decision"] for c in servicio.listar(db.RESUELTO)] == ["aprobar"]


def test_thread_inexistente_vuelve_a_la_bandeja(cliente):
    resp = cliente.get("/caso/care-noexiste", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/"


def test_pestana_de_resueltos_separa_los_cerrados(cliente):
    pendiente = _caso_nuevo("i")
    cerrado = _caso_nuevo("j")
    cliente.post(f"/caso/{cerrado}/decidir", data={**_campos(cerrado), "decision": "aprobar"})

    assert cerrado in cliente.get("/resueltos").text
    assert pendiente not in cliente.get("/resueltos").text
    assert pendiente in cliente.get("/").text


# --------------------------------------------------------------------------- #
# Friccion ante validaciones bloqueantes
# --------------------------------------------------------------------------- #
def _caso_bloqueado(sufijo: str) -> str:
    """Un caso sobre la sede de Medellin, cuyo nodo esta saturado en el mini sistema."""
    correo = dict(
        CORREO,
        message_id=f"<blq-{sufijo}@mail.acme.com.co>",
        cuerpo="Necesitamos ampliar el ancho de banda de la sede de Medellin a 500 Mbps.",
    )
    res = servicio.procesar_correo(correo)
    assert res["estado"] == "pendiente"
    return res["thread_id"]


def test_el_caso_de_medellin_trae_una_validacion_bloqueante():
    """Sin esto las demas pruebas de friccion pasarian por vacuidad."""
    caso = servicio.detalle(_caso_bloqueado("a"))["caso"]
    assert [v["check"] for v in bloqueantes_de(caso)] == ["capacidad_nodo"]


def test_aprobar_sin_confirmar_el_bloqueo_no_aplica_la_decision(cliente):
    thread = _caso_bloqueado("b")
    r = cliente.post(f"/caso/{thread}/decidir",
                     data={**_campos(thread), "decision": "aprobar", "comentario": "va igual"})

    assert "No se aplico la decision" in r.text
    assert "marca la casilla de confirmacion" in r.text
    assert servicio.detalle(thread)["pendiente"] is True     # sigue en la bandeja


def test_confirmar_el_bloqueo_sin_motivo_tampoco_basta(cliente):
    """La casilla sola seria un click mas, no una decision consciente."""
    thread = _caso_bloqueado("c")
    r = cliente.post(f"/caso/{thread}/decidir",
                     data={**_campos(thread), "decision": "aprobar",
                           "confirmo_bloqueo": "1", "comentario": "   "})

    assert "escribe el motivo" in r.text
    assert servicio.detalle(thread)["pendiente"] is True


def test_con_casilla_y_motivo_se_aprueba(cliente):
    thread = _caso_bloqueado("d")
    cliente.post(f"/caso/{thread}/decidir",
                 data={**_campos(thread), "decision": "aprobar", "confirmo_bloqueo": "1",
                       "comentario": "Ingenieria confirmo capacidad por otra via."})

    det = servicio.detalle(thread)
    assert det["pendiente"] is False
    assert det["fila"]["comentario"] == "Ingenieria confirmo capacidad por otra via."


def test_rechazar_un_caso_bloqueado_no_pide_confirmacion(cliente):
    """Rechazar es la salida segura: ponerle friccion empujaria a aprobar."""
    thread = _caso_bloqueado("e")
    cliente.post(f"/caso/{thread}/decidir", data={"decision": "rechazar", "comentario": "no aplica"})

    assert servicio.detalle(thread)["resultado"]["estado"] == "descartado"


def test_el_rechazo_de_validacion_no_borra_lo_editado(cliente):
    """Si el guardarrail le borra el trabajo al ejecutivo, deja de usarlo."""
    thread = _caso_bloqueado("f")
    datos = _campos(thread)
    datos["sel"] = datos["sel"][:1]                     # desmarca todas menos una
    clave = next(k for k in datos if k.startswith("par::"))
    datos[clave] = "EDITADO-POR-EL-EJECUTIVO"

    r = cliente.post(f"/caso/{thread}/decidir",
                     data={**datos, "decision": "aprobar", "comentario": "con motivo"})

    assert "No se aplico la decision" in r.text
    assert "EDITADO-POR-EL-EJECUTIVO" in r.text          # el parametro vuelve
    assert "con motivo" in r.text                        # el comentario tambien

    # Y las casillas vuelven como las dejo: una marcada, el resto no.
    casillas = re.findall(r'<input type="checkbox" name="sel"[^>]*>', r.text)
    assert len(casillas) == len(_campos(thread)["sel"])
    assert sum("checked" in c for c in casillas) == 1


def test_un_caso_sin_bloqueantes_no_pide_nada(cliente):
    """La friccion no debe contagiarse a los casos limpios."""
    thread = _caso_nuevo("limpio")
    assert bloqueantes_de(servicio.detalle(thread)["caso"]) == []

    cliente.post(f"/caso/{thread}/decidir", data={**_campos(thread), "decision": "aprobar"})
    assert servicio.detalle(thread)["pendiente"] is False
