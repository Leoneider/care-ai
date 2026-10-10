"""Pruebas del borrador de respuesta al cliente.

Nada aqui toca la red: `crear_borrador` se prueba contra un doble que captura la
peticion, que es donde estan los detalles que importan (el hilo, los headers de
respuesta, el asunto). La regla que mas se cuida: un fallo redactando o creando
el borrador NO puede tumbar una decision que el ejecutivo ya tomo.
"""

import base64
import email
import email.policy

import pytest

from src.agente import respuesta, servicio
from src.agente.fakes import CORREO_EJEMPLO as CORREO
from src.poller.gmail_ingesta import crear_borrador


# --------------------------------------------------------------------------- #
# Doble del cliente de Gmail
# --------------------------------------------------------------------------- #
class _Drafts:
    def __init__(self, bitacora):
        self.bitacora = bitacora

    def create(self, userId, body):
        self.bitacora.append({"userId": userId, "body": body})
        return self

    def execute(self):
        return {"id": "r-777", "message": {"id": "m-777"}}


class FakeGmail:
    """Captura lo que se le manda, sin salir a la red."""

    def __init__(self):
        self.peticiones: list[dict] = []

    def users(self):
        return self

    def drafts(self):
        return _Drafts(self.peticiones)

    # lo que se envio, ya desempaquetado
    def mensaje(self):
        crudo = self.peticiones[-1]["body"]["message"]["raw"]
        # policy=default para recuperar un EmailMessage y no el Message legacy,
        # que no sabe devolver el cuerpo con get_content().
        return email.message_from_bytes(base64.urlsafe_b64decode(crudo), policy=email.policy.default)


def _correo_gmail(**extra):
    return dict(CORREO, thread_id="THR-GMAIL-99", **extra)


# --------------------------------------------------------------------------- #
# Construccion del borrador
# --------------------------------------------------------------------------- #
def test_el_borrador_va_dentro_del_hilo_original():
    """Sin threadId y sin In-Reply-To, la respuesta aparece como correo suelto y
    el cliente no la asocia a lo que pregunto."""
    svc = FakeGmail()
    crear_borrador(svc, _correo_gmail(), "Ampliacion sede Bogota", "Hola, falta la fecha.")

    peticion = svc.peticiones[-1]["body"]["message"]
    assert peticion["threadId"] == "THR-GMAIL-99"

    msg = svc.mensaje()
    assert msg["In-Reply-To"] == CORREO["message_id"]
    assert msg["References"] == CORREO["message_id"]
    assert msg["To"] == CORREO["remitente"]
    assert msg.get_content().strip() == "Hola, falta la fecha."


def test_el_asunto_lleva_un_solo_Re():
    svc = FakeGmail()
    crear_borrador(svc, _correo_gmail(), "Ampliacion sede", "x")
    assert svc.mensaje()["Subject"] == "Re: Ampliacion sede"

    crear_borrador(svc, _correo_gmail(), "Re: Ampliacion sede", "x")
    assert svc.mensaje()["Subject"] == "Re: Ampliacion sede"


def test_sin_message_id_real_no_se_inventa_el_ancla():
    """Un 'gmail:18f2a' sintetico no es un Message-ID valido; ponerlo como
    In-Reply-To rompe el enhebrado en vez de arreglarlo."""
    svc = FakeGmail()
    crear_borrador(svc, _correo_gmail(message_id="gmail:18f2a"), "Asunto", "x")

    msg = svc.mensaje()
    assert msg["In-Reply-To"] is None and msg["References"] is None
    assert svc.peticiones[-1]["body"]["message"]["threadId"] == "THR-GMAIL-99"


# --------------------------------------------------------------------------- #
# Que acciones disparan un borrador
# --------------------------------------------------------------------------- #
def test_solo_las_acciones_de_sistema_CORREO_cuentan():
    acciones = [
        {"codigo": "COTIZAR_UPGRADE", "sistema": "CRM"},
        {"codigo": "SOLICITAR_DATO_FALTANTE", "sistema": "CORREO"},
        {"codigo": "COMPARAR_FACTURA_VS_CONTRATO", "sistema": "FACTURACION"},
    ]
    assert [a["codigo"] for a in respuesta.acciones_de_correo(acciones)] == ["SOLICITAR_DATO_FALTANTE"]


def test_en_fase_2_el_cierre_responde_aunque_no_haya_accion_de_correo(monkeypatch):
    """En la Fase 1 solo habia borrador si una accion aprobada era de CORREO.
    Con la ejecucion real, cerrar el caso es tambien contarle al cliente que se
    hizo: la respuesta de cierre sale siempre."""
    svc = FakeGmail()
    monkeypatch.setenv("CARE_BORRADOR_GMAIL", "1")
    monkeypatch.setattr("src.poller.gmail_ingesta.cliente", lambda *a, **k: svc)
    res = servicio.procesar_correo(dict(CORREO, message_id="<rsp-1@ufpso.edu.co>"))
    caso = servicio.detalle(res["thread_id"])["caso"]
    solo_sistemas = [a for s in caso["solicitudes"] for a in s["acciones"] if a["sistema"] != "CORREO"]

    final = servicio.resolver(res["thread_id"], "editar", "sin correo al cliente", acciones_finales=solo_sistemas)
    assert final["borrador"]["estado"] == "creado"


def test_rechazar_nunca_escribe_al_cliente(monkeypatch):
    monkeypatch.setenv("CARE_BORRADOR_GMAIL", "1")
    res = servicio.procesar_correo(dict(CORREO, message_id="<rsp-2@ufpso.edu.co>"))
    final = servicio.resolver(res["thread_id"], "rechazar", "no es del cliente")
    assert "borrador" not in final


def test_apagado_por_defecto(monkeypatch):
    """Escribir en el buzon tiene que ser una decision explicita."""
    monkeypatch.delenv("CARE_BORRADOR_GMAIL", raising=False)
    res = servicio.procesar_correo(dict(CORREO, message_id="<rsp-3@ufpso.edu.co>"))
    final = servicio.resolver(res["thread_id"], "aprobar")
    assert final["borrador"]["estado"] == "desactivado"


# --------------------------------------------------------------------------- #
# Camino completo y modos de fallo
# --------------------------------------------------------------------------- #
def test_aprobar_deja_el_borrador_redactado(monkeypatch):
    svc = FakeGmail()
    monkeypatch.setenv("CARE_BORRADOR_GMAIL", "1")
    monkeypatch.setattr("src.poller.gmail_ingesta.cliente", lambda *a, **k: svc)

    res = servicio.procesar_correo(dict(CORREO, message_id="<rsp-4@ufpso.edu.co>"))
    final = servicio.resolver(res["thread_id"], "aprobar", "ok")

    assert final["borrador"]["estado"] == "creado"
    assert final["borrador"]["draft_id"] == "r-777"
    assert "nota credito" in final["borrador"]["cuerpo"]       # la respuesta de cierre
    assert svc.mensaje()["To"] == CORREO["remitente"]


@pytest.mark.parametrize("donde", ["src.poller.gmail_ingesta.cliente", "src.agente.respuesta.redactar"])
def test_un_fallo_con_gmail_no_tumba_la_decision(monkeypatch, donde):
    """El caso ya se resolvio y se registro. Un fallo aqui deja al ejecutivo sin
    borrador, no sin caso: si propagara, el thread quedaria cerrado en el grafo
    pero la UI mostraria un error 500 y nadie sabria en que estado quedo."""
    monkeypatch.setenv("CARE_BORRADOR_GMAIL", "1")
    monkeypatch.setattr(donde, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("403 insufficient scope")))

    res = servicio.procesar_correo(dict(CORREO, message_id=f"<rsp-5{donde[-4:]}@ufpso.edu.co>"))
    final = servicio.resolver(res["thread_id"], "aprobar", "ok")

    assert final["estado"] == "resuelto"                      # la decision vale
    assert final["resultado"]["estado"] == "ejecutado"
    assert final["borrador"]["estado"] == "error"
    assert "403" in final["borrador"]["detalle"]
    assert servicio.detalle(res["thread_id"])["pendiente"] is False


# --------------------------------------------------------------------------- #
# El borrador tiene que sobrevivir al redirect y verse en la UI
# --------------------------------------------------------------------------- #
def test_la_migracion_agrega_columnas_sin_perder_filas(tmp_path, monkeypatch):
    """Una BD creada antes de que existieran esas columnas. Sin `_migrar`, la
    primera consulta muere con 'no such column: borrador_estado'."""
    import sqlite3

    from src.agente import db

    ruta = tmp_path / "vieja.db"
    monkeypatch.setenv("CARE_DB", str(ruta))

    # `ESQUEMA` es justo la tabla SIN las columnas nuevas: lo que hay en una BD
    # creada antes de este cambio. Las agregadas despues viven en COLUMNAS_NUEVAS.
    con = sqlite3.connect(ruta)
    con.executescript(db.ESQUEMA)
    con.execute(
        "INSERT INTO caso (thread_id, message_id, estado, creado_en)"
        " VALUES ('t1', '<m1>', 'pendiente', 'ayer')"
    )
    con.commit()
    con.close()

    assert "borrador_estado" not in {r[1] for r in sqlite3.connect(ruta).execute("PRAGMA table_info(caso)")}

    with db.conexion() as c:                                # aqui corre _migrar
        cols = {r["name"] for r in c.execute("PRAGMA table_info(caso)")}
        assert set(db.COLUMNAS_NUEVAS) <= cols, "no agrego las columnas nuevas"
        assert c.execute("SELECT COUNT(*) FROM caso").fetchone()[0] == 1, "perdio filas"
        # y la fila vieja queda utilizable, con el valor por defecto
        assert c.execute("SELECT borrador_estado FROM caso WHERE thread_id='t1'").fetchone()[0] == ""

    # Idempotente: abrir de nuevo no debe intentar re-agregarlas.
    with db.conexion() as c:
        assert c.execute("SELECT COUNT(*) FROM caso").fetchone()[0] == 1


def test_el_borrador_creado_se_ve_en_el_caso_resuelto(monkeypatch):
    from fastapi.testclient import TestClient

    from src.web.app import app

    svc = FakeGmail()
    monkeypatch.setenv("CARE_BORRADOR_GMAIL", "1")
    monkeypatch.setattr("src.poller.gmail_ingesta.cliente", lambda *a, **k: svc)

    res = servicio.procesar_correo(dict(CORREO, message_id="<rsp-ui@ufpso.edu.co>"))
    thread = res["thread_id"]
    with TestClient(app) as c:
        caso = servicio.detalle(thread)["caso"]
        datos = {"sel": [], "decision": "aprobar", "comentario": ""}
        for s in caso["solicitudes"]:
            for i, a in enumerate(s["acciones"]):
                datos["sel"].append(f"{s['id']}::{i}")
                datos[f"desc::{s['id']}::{i}"] = a["descripcion"]
                for k, v in (a["parametros"] or {}).items():
                    datos[f"par::{s['id']}::{i}::{k}"] = v
        c.post(f"/caso/{thread}/decidir", data=datos)

        # El POST redirige: si el borrador solo viviera en esa respuesta, aqui no
        # quedaria rastro y el ejecutivo no sabria que tiene algo en Gmail.
        html = c.get(f"/caso/{thread}").text

    assert "Borrador de respuesta creado" in html
    assert servicio.detalle(thread)["fila"]["borrador_id"] == "r-777"
