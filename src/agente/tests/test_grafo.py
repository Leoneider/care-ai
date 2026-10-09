"""Pruebas minimas de topologia. El modo fake y la BD temporal salen del conftest."""

from langgraph.types import Command

from src.agente.demo import CORREO
from src.agente.graph import construir_grafo


def _correr(correo, decision, thread):
    app = construir_grafo()
    cfg = {"configurable": {"thread_id": thread}}
    est = app.invoke({"correo": correo}, config=cfg)
    if "__interrupt__" not in est:
        return est, None
    payload = est["__interrupt__"][0].value
    return app.invoke(Command(resume=decision), config=cfg), payload


def test_fan_out_detecta_dos_solicitudes():
    _, payload = _correr(CORREO, {"decision": "aprobar"}, "t1")
    assert len(payload["caso"]["solicitudes"]) == 2


def test_pausa_en_revision_humana():
    _, payload = _correr(CORREO, {"decision": "aprobar"}, "t2")
    assert payload["tipo"] == "revision_care"
    assert payload["caso"]["cuenta"] == "B2B-005130"


def test_rechazo_no_ejecuta_acciones():
    final, _ = _correr(CORREO, {"decision": "rechazar", "comentario": "spam"}, "t3")
    assert final["resultado"]["estado"] == "descartado"


def test_idempotencia_descarta_duplicado():
    _correr(CORREO, {"decision": "aprobar"}, "t4a")
    final, payload = _correr(CORREO, {"decision": "aprobar"}, "t4b")
    assert payload is None and final["duplicado"] is True


def test_cliente_desconocido_marca_atencion():
    correo = dict(CORREO, message_id="<otro-1@desconocido.io>", remitente="juan@desconocido.io")
    _, payload = _correr(correo, {"decision": "aprobar"}, "t5")
    assert payload["caso"]["requiere_atencion"] is True
    assert payload["caso"]["cuenta"] == "NO IDENTIFICADA"


def test_validacion_detecta_diferencia_de_factura():
    _, payload = _correr(CORREO, {"decision": "aprobar"}, "t6")
    fact = next(s for s in payload["caso"]["solicitudes"] if s["tipo"] == "facturacion_inconsistencia")
    checks = {v["check"]: v["resultado"] for v in fact["validaciones"]}
    assert checks["factura_vs_contrato"] == "alerta"
