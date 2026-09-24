"""Pruebas de la capa de servicio: el contrato que usan la UI y el poller.

Lo que se cubre aqui no es el grafo (eso es `test_grafo.py`) sino la costura
nueva: que el caso quede listable, que se pueda reanudar desde "otro proceso" y
que la idempotencia aguante barridos repetidos.
"""

import pytest

from src import db, servicio
from src.demo import CORREO


def _otro_correo(sufijo: str, **campos):
    return dict(CORREO, message_id=f"<{sufijo}@mail.acme.com.co>", **campos)


def test_procesar_deja_el_caso_en_la_bandeja():
    res = servicio.procesar_correo(_otro_correo("c1"))

    assert res["estado"] == "pendiente"
    pendientes = servicio.listar(db.PENDIENTE)
    assert [c["thread_id"] for c in pendientes] == [res["thread_id"]]
    assert pendientes[0]["cuenta"] == "B2B-004712"
    assert pendientes[0]["n_solicitudes"] == 2


def test_detalle_trae_el_caso_completo_del_checkpointer():
    """La bandeja solo guarda el encabezado; validaciones y acciones salen del
    estado del thread, que es lo que la vista de detalle necesita."""
    res = servicio.procesar_correo(_otro_correo("c2"))
    det = servicio.detalle(res["thread_id"])

    assert det["pendiente"] is True
    assert det["opciones"] == ["aprobar", "editar", "rechazar"]
    sol = det["caso"]["solicitudes"][0]
    assert sol["validaciones"] and sol["acciones"]
    assert det["correo"]["message_id"] == "<c2@mail.acme.com.co>"


def test_aprobar_ejecuta_lo_propuesto_y_cierra_el_caso():
    res = servicio.procesar_correo(_otro_correo("c3"))
    final = servicio.resolver(res["thread_id"], "aprobar", "todo bien")

    assert final["estado"] == "resuelto"
    assert final["resultado"]["estado"] == "ejecutado"
    assert servicio.listar(db.PENDIENTE) == []
    assert [c["decision"] for c in servicio.listar(db.RESUELTO)] == ["aprobar"]


def test_editar_guarda_solo_las_acciones_finales():
    """La edicion del humano es la etiqueta de oro del dataset: si el ejecutivo
    deja una accion de cuatro, se ejecuta una, no cuatro."""
    res = servicio.procesar_correo(_otro_correo("c4"))
    caso = servicio.detalle(res["thread_id"])["caso"]
    una = caso["solicitudes"][0]["acciones"][:1]

    final = servicio.resolver(res["thread_id"], "editar", "sobra la segunda", acciones_finales=una)

    assert [a["codigo"] for a in final["resultado"]["acciones"]] == [una[0]["codigo"]]


def test_rechazar_no_ejecuta_nada():
    res = servicio.procesar_correo(_otro_correo("c5"))
    final = servicio.resolver(res["thread_id"], "rechazar", "no es del cliente")

    assert final["resultado"] == {"estado": "descartado", "motivo": "no es del cliente"}


def test_el_caso_resuelto_sigue_siendo_legible():
    res = servicio.procesar_correo(_otro_correo("c6"))
    servicio.resolver(res["thread_id"], "aprobar")
    det = servicio.detalle(res["thread_id"])

    assert det["pendiente"] is False
    assert det["caso"]["solicitudes"]          # la UI lo pinta en modo lectura
    assert det["resultado"]["estado"] == "ejecutado"


def test_segundo_barrido_no_duplica_el_caso():
    """El poller vuelve a ver el mismo correo cada 60 s. Un Message-ID, un caso."""
    correo = _otro_correo("c7")
    primero = servicio.procesar_correo(correo)
    segundo = servicio.procesar_correo(correo)

    assert primero["estado"] == "pendiente"
    assert segundo["estado"] == "duplicado"
    assert len(servicio.listar(db.PENDIENTE)) == 1


def test_resolver_dos_veces_no_reejecuta_el_grafo():
    """Doble submit en la UI, o dos ejecutivos sobre el mismo caso."""
    res = servicio.procesar_correo(_otro_correo("c8"))
    servicio.resolver(res["thread_id"], "aprobar")

    assert servicio.resolver(res["thread_id"], "rechazar")["estado"] == "ya_resuelto"
    assert [c["decision"] for c in servicio.listar(db.RESUELTO)] == ["aprobar"]


def test_un_fallo_libera_la_reserva_para_el_siguiente_barrido(monkeypatch):
    """Si no se liberara, un error transitorio del modelo perderia el correo
    para siempre: la reserva lo marcaria como visto sin haber dejado caso."""
    correo = _otro_correo("c9")
    app = servicio.app_care()
    monkeypatch.setattr(app, "invoke", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("503 del modelo")))

    fallido = servicio.procesar_correo(correo)
    assert fallido["estado"] == "error"

    monkeypatch.undo()
    assert servicio.procesar_correo(correo)["estado"] == "pendiente"


def test_correos_del_mismo_hilo_de_gmail_no_comparten_thread():
    """Dos mensajes del mismo hilo son dos casos. Reusar el threadId de Gmail
    haria que el segundo pisara el checkpoint del primero."""
    a = servicio.procesar_correo(_otro_correo("h1", thread_id="THR-GMAIL-1"))
    b = servicio.procesar_correo(_otro_correo("h2", thread_id="THR-GMAIL-1"))

    assert a["thread_id"] != b["thread_id"]
    assert len(servicio.listar(db.PENDIENTE)) == 2


def test_decision_desconocida_se_rechaza():
    res = servicio.procesar_correo(_otro_correo("c10"))
    with pytest.raises(ValueError, match="Decision invalida"):
        servicio.resolver(res["thread_id"], "ejecutar_todo_sin_revisar")
