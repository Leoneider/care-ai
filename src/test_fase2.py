"""Fase 2: ejecucion en el mini sistema, verificacion y cierre.

El caso central es el correo de la demo desde la cuenta UFPSO: ampliar Bogota a
500 Mbps y reclamar la factura de septiembre. Tiene que terminar con los datos
cambiados de verdad en los sistemas, los tickets resueltos y el caso cerrado sin
que nadie lo toque. Las demas pruebas cubren lo que NO debe cerrarse solo.
"""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from src import backends, db, ejecucion, servicio, sistemas, validaciones
from src.demo import CORREO
from src.prompts import SISTEMA_POR_ACCION
from src.schemas import RespuestaCliente
from src.web.app import app


def _ufpso(sufijo: str, **extra):
    return dict(
        CORREO,
        message_id=f"<ufpso-{sufijo}@ufpso.edu.co>",
        remitente="Leoneider Trigos <ltrigosg@ufpso.edu.co>",
        asunto="CARE-TEST Ampliacion sede Bogota + revision factura septiembre",
        **extra,
    )


def _aprobado(sufijo: str, **extra) -> dict:
    res = servicio.procesar_correo(_ufpso(sufijo, **extra))
    assert res["estado"] == "pendiente"
    return {**servicio.resolver(res["thread_id"], "aprobar", "ok"), "thread_id": res["thread_id"]}


# --------------------------------------------------------------------------- #
# El mini sistema
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("direccion", ["ltrigosg@ufpso.edu.co", "ltrigos@ufpso.edu.co"])
def test_las_dos_direcciones_de_ufpso_son_contacto_autorizado(direccion):
    c = backends.crm_buscar_cuenta_por_correo(direccion)
    assert c["cuenta_id"] == "B2B-005130"
    assert c["contacto_autorizado"] is True


def test_otra_direccion_del_dominio_identifica_la_cuenta_pero_no_autoriza():
    c = backends.crm_buscar_cuenta_por_correo("alguien@ufpso.edu.co")
    assert c["encontrado"] is True and c["contacto_autorizado"] is False


def test_el_sistema_se_defiende_de_un_upgrade_que_no_cabe():
    """Aunque el copiloto se equivoque, el sistema no deja romper el nodo."""
    with pytest.raises(sistemas.ErrorSistema, match="no admite"):
        sistemas.aplicar_ancho_banda("SVC-9311", 300)          # Cucuta, nodo saturado
    assert sistemas.servicio("SVC-9311")["ancho_banda_mbps"] == 100
    assert sistemas.operaciones("B2B-005130") == []            # nada que registrar


def test_restaurar_deshace_los_cambios_y_vacia_la_bitacora():
    sistemas.aplicar_ancho_banda("SVC-9310", 500, "CASE-X")
    assert sistemas.operaciones("B2B-005130")
    sistemas.restaurar()
    assert sistemas.servicio("SVC-9310")["ancho_banda_mbps"] == 200
    assert sistemas.operaciones("B2B-005130") == []


# --------------------------------------------------------------------------- #
# Leer lo que extrae el modelo
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("texto,esperado", [
    ("septiembre", "2026-09"),
    ("Septiembre de 2025", "2025-09"),
    ("sept", "2026-09"),
    ("09/2026", "2026-09"),
    ("2026-9", "2026-09"),
    ("diciembre", "2025-12"),        # en septiembre, "diciembre" es el del ano pasado
])
def test_el_periodo_se_entiende_en_palabras(texto, esperado):
    assert validaciones.normalizar_periodo(texto, hoy=date(2026, 9, 23)) == esperado


@pytest.mark.parametrize("texto,esperado", [
    ("500", 500), ("500 Mbps", 500), ("1.000 Mbps", 1000), ("1 Gbps", 1000), ("", 0),
])
def test_el_ancho_de_banda_se_lee_con_unidades(texto, esperado):
    assert validaciones.numero(texto) == esperado


def test_la_sede_se_encuentra_con_tilde_o_dentro_de_una_frase():
    cliente = backends.crm_buscar_cuenta_por_correo("ltrigosg@ufpso.edu.co")
    for sede in ("Bogotá", "sede de Bogota", "BOGOTA"):
        assert validaciones.buscar_servicio(cliente, sede)["servicio_id"] == "SVC-9310"


def test_una_ciudad_no_encuentra_otra_por_parecido():
    """'Cali' no puede encontrar 'Bogota - Calle 100' por compartir letras."""
    cliente = backends.crm_buscar_cuenta_por_correo("laura.gomez@acme.com.co")
    assert validaciones.buscar_servicio(cliente, "Cali") is None


def test_el_sistema_de_cada_accion_lo_fija_el_catalogo():
    res = servicio.procesar_correo(_ufpso("sis"))
    for s in res["caso"]["solicitudes"]:
        for a in s["acciones"]:
            assert a["sistema"] == SISTEMA_POR_ACCION[a["codigo"]]


# --------------------------------------------------------------------------- #
# El correo de la demo, de punta a punta
# --------------------------------------------------------------------------- #
def test_el_correo_de_la_demo_se_ejecuta_verifica_y_cierra_solo():
    final = _aprobado("a")

    assert final["estado"] == "resuelto"
    assert final["resultado"]["cierre"]["modo"] == "automatico"
    assert final["verificacion"]["checks"] and all(c["ok"] for c in final["verificacion"]["checks"])

    # Los datos cambiaron de verdad en los sistemas.
    assert sistemas.servicio("SVC-9310")["ancho_banda_mbps"] == 500
    assert sistemas.nodo("BOG-N21")["capacidad_libre_mbps"] == 500
    factura = sistemas.factura("FE-91502")
    assert factura["ajustes"] == 950_000 and factura["valor_neto"] == 6_200_000

    # Un ticket de seguimiento por solicitud, ambos resueltos.
    tickets = sistemas.tickets("B2B-005130")
    assert len(tickets) == 2 and {t["estado"] for t in tickets} == {"resuelto"}
    assert db.obtener(final["thread_id"])["cierre"] == "automatico"


def test_el_mismo_reclamo_otra_vez_ya_no_tiene_sustento():
    """Tras la nota credito la factura cuadra: la validacion compara el neto."""
    _aprobado("b1")
    otra = servicio.procesar_correo(_ufpso("b2"))
    sols = {s["tipo"]: s for s in otra["caso"]["solicitudes"]}

    fact = {v["check"]: v for v in sols["facturacion_inconsistencia"]["validaciones"]}
    assert fact["factura_vs_contrato"]["resultado"] == "ok"
    ampl = {v["check"]: v for v in sols["ampliacion_ancho_banda"]["validaciones"]}
    assert ampl["delta_ancho_banda"]["bloqueante"] is True     # ya tiene 500 Mbps


def test_el_monto_de_la_nota_credito_no_sale_del_modelo():
    """Los parametros del modelo son pistas: cuanto acreditar lo dicen facturacion
    y contratos."""
    res = servicio.procesar_correo(_ufpso("c"))
    finales = [dict(a, parametros={"valor": "99999999"})
               for s in res["caso"]["solicitudes"] for a in s["acciones"]]
    servicio.resolver(res["thread_id"], "editar", "", acciones_finales=finales)
    assert [n["valor"] for n in sistemas.notas_credito("B2B-005130")] == [950_000]


def test_un_rechazo_no_toca_los_sistemas():
    res = servicio.procesar_correo(_ufpso("d"))
    servicio.resolver(res["thread_id"], "rechazar", "no aplica")
    assert sistemas.servicio("SVC-9310")["ancho_banda_mbps"] == 200
    assert sistemas.operaciones("B2B-005130") == []


# --------------------------------------------------------------------------- #
# Lo que no debe cerrarse solo
# --------------------------------------------------------------------------- #
def test_una_nota_credito_sobre_el_tope_pasa_por_el_ejecutivo(monkeypatch):
    monkeypatch.setenv("CARE_TOPE_NOTA_CREDITO", "500000")
    final = _aprobado("e")

    assert final["estado"] == db.EN_VERIFICACION
    assert any("supera el tope" in m for m in final["verificacion"]["motivos"])
    # Ya se ejecuto: la nota existe aunque el caso no este cerrado.
    assert sistemas.factura("FE-91502")["valor_neto"] == 6_200_000
    assert [c["thread_id"] for c in servicio.listar(db.EN_VERIFICACION)] == [final["thread_id"]]
    assert servicio.detalle(final["thread_id"])["etapa"] == "verificacion"

    cierre = servicio.cerrar(final["thread_id"], "cerrar", "revisado con facturacion")
    assert cierre["estado"] == "resuelto"
    assert cierre["resultado"]["cierre"]["modo"] == "ejecutivo"
    assert {t["estado"] for t in sistemas.tickets("B2B-005130")} == {"resuelto"}
    fila = db.obtener(final["thread_id"])
    assert fila["cierre"] == "ejecutivo:cerrar" and fila["decision"] == "aprobar"


def test_dejar_abierto_pone_los_tickets_en_espera(monkeypatch):
    monkeypatch.setenv("CARE_TOPE_NOTA_CREDITO", "1")
    final = _aprobado("f")
    servicio.cerrar(final["thread_id"], "dejar_abierto", "esperando confirmacion del cliente")

    assert {t["estado"] for t in sistemas.tickets("B2B-005130")} == {"en_espera"}
    assert servicio.detalle(final["thread_id"])["resultado"]["cierre"]["estado"] == "abierto"


def test_un_caso_en_verificacion_no_se_vuelve_a_procesar(monkeypatch):
    monkeypatch.setenv("CARE_TOPE_NOTA_CREDITO", "1")
    _aprobado("g")
    assert servicio.procesar_correo(_ufpso("g"))["estado"] == "duplicado"


def test_cerrar_dos_veces_no_reejecuta(monkeypatch):
    monkeypatch.setenv("CARE_TOPE_NOTA_CREDITO", "1")
    final = _aprobado("h")
    servicio.cerrar(final["thread_id"], "cerrar")
    assert servicio.cerrar(final["thread_id"], "dejar_abierto")["estado"] == "ya_resuelto"


def test_aprobar_pese_a_un_bloqueo_pasa_por_verificacion_y_no_aplica_el_upgrade():
    correo = dict(CORREO, message_id="<blq-f2@mail.acme.com.co>",
                  cuerpo="Necesitamos ampliar el ancho de banda de la sede de Medellin a 500 Mbps.")
    res = servicio.procesar_correo(correo)
    final = servicio.resolver(res["thread_id"], "aprobar", "ingenieria lo revisa")

    assert final["estado"] == db.EN_VERIFICACION
    motivos = " ".join(final["verificacion"]["motivos"])
    assert "validacion bloqueante" in motivos and "Ingenieria" in motivos
    assert sistemas.servicio("SVC-8892")["ancho_banda_mbps"] == 100


def test_si_el_sistema_rechaza_la_operacion_el_caso_no_cierra_solo():
    cliente = backends.crm_buscar_cuenta_por_correo("ltrigosg@ufpso.edu.co")
    caso = {"caso_id": "CASE-T", "cuenta": "B2B-005130", "solicitudes": [{
        "id": "s1", "tipo": "ampliacion_ancho_banda", "resumen": "Cucuta a 300",
        "datos": {"sede": "Cucuta", "ancho_banda_objetivo_mbps": "300"},
        "faltantes": [], "validaciones": [],
        "acciones": [{"codigo": "APLICAR_UPGRADE_ANCHO_BANDA", "sistema": "RED"}],
    }]}
    r = ejecucion.ejecutar(caso, cliente, caso["solicitudes"][0]["acciones"])

    assert r["acciones"][0]["estado"] == "error" and "no admite" in r["acciones"][0]["detalle"]
    assert sistemas.servicio("SVC-9311")["ancho_banda_mbps"] == 100
    assert ejecucion.evaluar_cierre(caso, r, ejecucion.verificar(r))


# --------------------------------------------------------------------------- #
# Gmail al cerrar
# --------------------------------------------------------------------------- #
class _Peticion:
    def __init__(self, valor):
        self.valor = valor

    def execute(self):
        return self.valor


class GmailDoble:
    """Borradores, etiquetas e hilos, sin red."""

    def __init__(self):
        self.borradores: list[dict] = []
        self.etiquetas: dict[str, str] = {}
        self.hilos: list[tuple[str, list[str]]] = []

    def users(self):
        return self

    def drafts(self):
        doble = self

        class Borradores:
            def create(self, userId, body):
                doble.borradores.append(body)
                return _Peticion({"id": "d-1"})
        return Borradores()

    def labels(self):
        doble = self

        class Etiquetas:
            def list(self, userId):
                return _Peticion({"labels": [{"name": n, "id": i} for n, i in doble.etiquetas.items()]})

            def create(self, userId, body):
                doble.etiquetas[body["name"]] = f"L{len(doble.etiquetas) + 1}"
                return _Peticion({"id": doble.etiquetas[body["name"]]})
        return Etiquetas()

    def threads(self):
        doble = self

        class Hilos:
            def modify(self, userId, id, body):
                doble.hilos.append((id, body["addLabelIds"]))
                return _Peticion({})
        return Hilos()


def test_al_cerrar_deja_la_respuesta_y_etiqueta_el_hilo(monkeypatch):
    svc = GmailDoble()
    monkeypatch.setenv("CARE_BORRADOR_GMAIL", "1")
    monkeypatch.setattr("src.gmail_ingesta.cliente", lambda *a, **k: svc)

    final = _aprobado("gm", thread_id="HILO-UFPSO")

    assert final["borrador"]["estado"] == "creado"
    assert "nota credito" in final["borrador"]["cuerpo"]
    assert final["etiqueta"] == "CareAI/Resuelto"
    assert svc.hilos == [("HILO-UFPSO", [svc.etiquetas["CareAI/Resuelto"]])]
    assert db.obtener(final["thread_id"])["etiqueta_cierre"] == "CareAI/Resuelto"


def test_dejar_abierto_responde_pero_no_da_el_correo_por_terminado(monkeypatch):
    svc = GmailDoble()
    monkeypatch.setenv("CARE_BORRADOR_GMAIL", "1")
    monkeypatch.setenv("CARE_TOPE_NOTA_CREDITO", "1")
    monkeypatch.setattr("src.gmail_ingesta.cliente", lambda *a, **k: svc)

    final = _aprobado("ga", thread_id="HILO-2")
    cierre = servicio.cerrar(final["thread_id"], "dejar_abierto", "falta confirmar")

    assert cierre["borrador"]["estado"] == "creado"
    assert cierre["etiqueta"] == "" and svc.hilos == []


def test_la_respuesta_de_cierre_recibe_las_cifras_de_los_sistemas(monkeypatch):
    """El modelo redacta, pero los numeros los pone el sistema: si el prompt no
    los trae, el modelo los inventaria."""
    capturado: list = []

    class Redactor:
        def invoke(self, mensajes):
            capturado.append(mensajes[-1]["content"])
            return RespuestaCliente(asunto="x", cuerpo="y")

    monkeypatch.setenv("CARE_BORRADOR_GMAIL", "1")
    monkeypatch.setattr("src.respuesta.get_structured_llm", lambda *a, **k: Redactor())
    monkeypatch.setattr("src.gmail_ingesta.cliente", lambda *a, **k: GmailDoble())

    _aprobado("cf")
    texto = capturado[-1]
    assert "200 -> 500 Mbps" in texto
    assert "950,000 COP" in texto
    assert "Estado del caso: cerrado" in texto


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
@pytest.fixture
def cliente():
    with TestClient(app) as c:
        yield c


def test_el_caso_cerrado_solo_lo_dice_en_su_detalle(cliente):
    final = _aprobado("ui2")
    html = cliente.get(f"/caso/{final['thread_id']}").text
    assert "Correo terminado" in html and "cierre automatico" in html
    assert "verificado" in html
    # los tickets enlazan a la consola del sistema legado, que es otra app
    assert "http://127.0.0.1:8002/?cuenta=B2B-005130" in html


def test_la_bandeja_ya_no_sirve_los_sistemas(cliente):
    assert cliente.get("/sistemas").status_code == 404
    assert cliente.post("/demo/reiniciar", data={"confirmo": "1"}).status_code == 404
    assert 'href="/sistemas"' not in cliente.get("/").text


def test_un_caso_en_verificacion_se_cierra_desde_la_ui(cliente, monkeypatch):
    monkeypatch.setenv("CARE_TOPE_NOTA_CREDITO", "1")
    thread = _aprobado("ui3")["thread_id"]

    assert thread in cliente.get("/verificacion").text
    html = cliente.get(f"/caso/{thread}").text
    assert "falta tu cierre" in html and "supera el tope" in html

    cliente.post(f"/caso/{thread}/cerrar", data={"decision": "cerrar", "comentario": "revisado"})
    assert servicio.detalle(thread)["etapa"] == "cerrado"
    assert "Correo terminado" in cliente.get(f"/caso/{thread}").text


def test_dejar_el_ticket_abierto_exige_decir_por_que(cliente, monkeypatch):
    monkeypatch.setenv("CARE_TOPE_NOTA_CREDITO", "1")
    thread = _aprobado("ui4")["thread_id"]
    r = cliente.post(f"/caso/{thread}/cerrar", data={"decision": "dejar_abierto", "comentario": "  "})

    assert "escribe en el comentario" in r.text
    assert servicio.detalle(thread)["etapa"] == "verificacion"


def test_reiniciar_la_demo_vacia_todo_sin_reprocesar_correos_viejos(monkeypatch):
    """El buzon sigue teniendo los correos CARE-TEST de antes: tras reiniciar, el
    poller no debe volver a meterlos."""
    monkeypatch.setenv("CARE_TOPE_NOTA_CREDITO", "1")
    _aprobado("r2")                                      # queda en verificacion
    _aprobado("r3")
    servicio.procesar_correo(_ufpso("r4"))               # queda pendiente

    assert servicio.reiniciar_demo() == {"casos_archivados": 3}
    assert servicio.resumen() == {"pendientes": 0, "en_verificacion": 0, "resueltos": 0, "total": 0}
    assert sistemas.tickets("B2B-005130") == [] and sistemas.operaciones("B2B-005130") == []
    for sufijo in ("r2", "r3", "r4"):
        assert servicio.procesar_correo(_ufpso(sufijo))["estado"] == "duplicado"
    assert servicio.procesar_correo(_ufpso("nuevo"))["estado"] == "pendiente"
