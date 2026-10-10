"""Capa de aplicacion: lo unico que la UI y el poller de Gmail conocen del grafo.

La demo original hacia los dos tramos seguidos en la misma funcion. Eso ya no aplica: el
tramo 1 lo dispara el poller y el tramo 2 la UI, minutos u horas despues y desde
otro proceso. Lo que los une es el `thread_id` y el checkpointer.

Ni `web/` ni `gmail_ingesta.py` importan `graph`, `nodes` ni `Command`. Si manana
el grafo se parte en dos o el checkpointer pasa a Postgres, el cambio se agota en
este archivo.
"""

import hashlib
import os
import threading
from typing import Any

from langgraph.types import Command

from . import db
from .graph import construir_grafo

_APP = None
_LOCK = threading.Lock()


def app_care():
    """Grafo compilado, uno por proceso. Compilar es barato pero el checkpointer
    abre un archivo, y no queremos uno por peticion HTTP."""
    global _APP
    with _LOCK:
        if _APP is None:
            _APP = construir_grafo()
        return _APP


def reiniciar() -> None:
    """Olvida el grafo cacheado. Para los tests, que cambian CARE_DB entre casos."""
    global _APP
    with _LOCK:
        _APP = None


def reiniciar_demo() -> dict[str, int]:
    """Deja la demo como recien instalada: bandeja vacia y sistemas en la semilla.

    Los casos se archivan, no se borran (ver `db.ARCHIVADO`): un correo que ya se
    proceso no vuelve a entrar. Para repetir la demo se manda un correo nuevo.
    """
    from ..sistemas_web import sistemas

    archivados = vaciar_bandeja()
    sistemas.restaurar()
    return {"casos_archivados": archivados}


def vaciar_bandeja() -> int:
    """Solo la mitad de Care AI de `reiniciar_demo`: archiva los casos, no toca los
    sistemas. La usa la consola del legado (por HTTP) al restaurar."""
    return db.archivar_todo()


def thread_de(message_id: str) -> str:
    """Un thread de LangGraph por correo, derivado del Message-ID.

    Deliberadamente NO se usa el threadId de Gmail: varios correos del mismo hilo
    compartirian thread y el segundo pisaria el caso del primero, que es peor que
    no tenerlo. El hilo de Gmail se guarda como metadato en la bandeja para cuando
    se aborden conversaciones multi-correo (hoy fuera de alcance, ver README).
    """
    return "care-" + hashlib.sha1(message_id.encode("utf-8")).hexdigest()[:16]


def _traza(thread_id: str, etapa: str, asunto: str = "", **extra: Any) -> dict[str, Any]:
    """Nombre, tags y metadata de un tramo en LangSmith.

    Cada tramo es una traza aparte (se separan por el `interrupt`, o corren fuera
    del grafo). El prefijo numerado dice en que punto del caso va, y `thread_id`
    en la metadata es la clave con que la pestana Threads de LangSmith las agrupa.
    """
    modo = "fake" if os.getenv("CARE_FAKE_LLM") == "1" else "modelo-real"
    return {
        "run_name": f"care-ai | {etapa} | {asunto or '(sin asunto)'}",
        "tags": ["care-ai", etapa.split(" ", 1)[-1], modo],
        "metadata": {"thread_id": thread_id, "modo_llm": modo, **extra},
    }


def _config(thread_id: str, etapa: str = "", asunto: str = "", **extra: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
    if etapa:
        cfg |= _traza(thread_id, etapa, asunto, **extra)
    return cfg


def _asunto(thread_id: str) -> str:
    return (db.obtener(thread_id) or {}).get("asunto", "")


# --------------------------------------------------------------------------- #
# Tramo 1: correo -> caso pendiente de revision
# --------------------------------------------------------------------------- #
def procesar_correo(correo: dict[str, Any]) -> dict[str, Any]:
    """Corre de la ingesta al `interrupt` y deja el caso en la bandeja.

    Devuelve `{estado, thread_id, ...}` con estado en:
      - `pendiente`: hay caso esperando al ejecutivo.
      - `duplicado`: el Message-ID ya se habia visto; no se corrio el grafo.
      - `sin_caso` : el grafo termino sin pedir revision (hoy solo si el propio
                     nodo `ingesta` lo marco duplicado).
      - `error`    : el tramo fallo; la reserva se libera y el proximo barrido
                     lo reintenta.
    """
    message_id = correo.get("message_id", "")
    thread_id = thread_de(message_id)

    if not db.reservar(message_id, thread_id):
        return {"estado": "duplicado", "thread_id": thread_id, "message_id": message_id}

    try:
        cfg = _config(thread_id, "1 ingesta", correo.get("asunto", ""),
                      message_id=message_id, remitente=correo.get("remitente", ""))
        estado = app_care().invoke({"correo": correo}, config=cfg)
    except Exception as exc:
        db.liberar(message_id)
        return {"estado": "error", "thread_id": thread_id, "message_id": message_id, "error": str(exc)}

    if "__interrupt__" not in estado:
        db.liberar(message_id)
        return {
            "estado": "sin_caso",
            "thread_id": thread_id,
            "message_id": message_id,
            "traza": estado.get("traza", []),
        }

    caso = estado["__interrupt__"][0].value["caso"]
    # Se registra el correo ya normalizado por el nodo `ingesta`, no el crudo:
    # es el `recibido_en` que el nodo rellena si Gmail no trajo fecha.
    db.registrar_caso(thread_id, estado.get("correo", correo), caso)
    return {"estado": "pendiente", "thread_id": thread_id, "message_id": message_id, "caso": caso}


# --------------------------------------------------------------------------- #
# Lectura para la bandeja
# --------------------------------------------------------------------------- #
def listar(estado: str | None = db.PENDIENTE) -> list[dict[str, Any]]:
    return db.listar(estado)


def resumen() -> dict[str, int]:
    return db.resumen()


def detalle(thread_id: str) -> dict[str, Any] | None:
    """El caso completo, leido del checkpointer.

    La bandeja da el encabezado; el detalle necesita validaciones y acciones, y
    esas solo estan en el estado del thread. Si el caso ya se resolvio no queda
    `interrupt`, pero `values["caso"]` sigue ahi: por eso la vista de un caso
    cerrado se pinta igual, en modo lectura.
    """
    fila = db.obtener(thread_id)
    snap = app_care().get_state(_config(thread_id))
    if snap.created_at is None and fila is None:
        return None

    interrupts = list(getattr(snap, "interrupts", ()) or ())
    valores = snap.values or {}
    pausa = interrupts[0].value if interrupts else None
    caso = pausa["caso"] if pausa else valores.get("caso")
    if caso is None:
        return None

    # Dos pausas posibles. `pendiente` sigue significando lo de siempre (esperando
    # la decision de la Fase 1), porque es lo que la UI usa para habilitar el
    # formulario de aprobar / editar / rechazar.
    tipo = (pausa or {}).get("tipo")
    etapa = {"revision_care": "revision", "revision_cierre": "verificacion"}.get(tipo, "cerrado")

    return {
        "thread_id": thread_id,
        "fila": fila or {},
        "caso": caso,
        "etapa": etapa,
        "pendiente": etapa == "revision",
        "opciones": (pausa or {}).get("opciones", []),
        "correo": valores.get("correo", {}),
        "traza": valores.get("traza", []),
        "resultado": valores.get("resultado"),
        "verificacion": valores.get("verificacion"),
        "decision_humana": valores.get("decision_humana"),
        "decision_cierre": valores.get("decision_cierre"),
    }


# --------------------------------------------------------------------------- #
# Tramo 2: decision del ejecutivo -> ejecucion -> verificacion -> (cierre)
# --------------------------------------------------------------------------- #
DECISIONES = ("aprobar", "editar", "rechazar")
DECISIONES_CIERRE = ("cerrar", "dejar_abierto")


def _en_pausa(app, cfg, tipo: str) -> bool:
    return any(i.value.get("tipo") == tipo for i in (getattr(app.get_state(cfg), "interrupts", ()) or ()))


def resolver(
    thread_id: str,
    decision: str,
    comentario: str = "",
    acciones_finales: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Reanuda el thread con la decision del ejecutivo.

    Con la Fase 2 la reanudacion ejecuta y verifica. Si no hay riesgo el grafo
    cierra solo (`estado: resuelto`); si lo hay, se detiene otra vez en la
    revision de cierre (`estado: en_verificacion`) y el caso pasa a esa pestana.

    `acciones_finales` solo se manda en `editar`: es la etiqueta de oro del
    dataset (propuesto vs. final) y por eso no se rellena sola.
    """
    if decision not in DECISIONES:
        raise ValueError(f"Decision invalida: {decision!r}. Esperaba una de {DECISIONES}.")

    app = app_care()
    cfg = _config(thread_id, "2 decision", _asunto(thread_id), decision=decision)
    if not _en_pausa(app, cfg, "revision_care"):
        # Doble submit, o dos ejecutivos sobre el mismo caso. Reanudar un thread
        # sin interrupt lo volveria a correr desde el principio.
        return {"estado": "ya_resuelto", "thread_id": thread_id}

    payload: dict[str, Any] = {"decision": decision, "comentario": comentario}
    if decision == "editar":
        payload["acciones_finales"] = acciones_finales or []

    final = app.invoke(Command(resume=payload), config=cfg)
    salida = {
        "thread_id": thread_id,
        "decision": decision,
        "resultado": final.get("resultado", {}),
        "verificacion": final.get("verificacion"),
        "traza": final.get("traza", []),
    }

    if "__interrupt__" in final:
        db.marcar_en_verificacion(thread_id, decision, comentario)
        return {**salida, "estado": db.EN_VERIFICACION}

    cierre = "" if decision == "rechazar" else "automatico"
    db.cerrar_caso(thread_id, decision, comentario, cierre=cierre)
    salida["estado"] = "resuelto"
    if decision != "rechazar":
        salida |= _escribir_en_gmail(thread_id, final)
    return salida


def cerrar(thread_id: str, decision: str, comentario: str = "") -> dict[str, Any]:
    """El ejecutivo cierra un caso que quedo en revision de cierre."""
    if decision not in DECISIONES_CIERRE:
        raise ValueError(f"Decision de cierre invalida: {decision!r}. Esperaba una de {DECISIONES_CIERRE}.")

    app = app_care()
    cfg = _config(thread_id, "2b cierre", _asunto(thread_id), decision=decision)
    if not _en_pausa(app, cfg, "revision_cierre"):
        return {"estado": "ya_resuelto", "thread_id": thread_id}

    final = app.invoke(Command(resume={"decision": decision, "comentario": comentario}), config=cfg)
    fila = db.obtener(thread_id) or {}
    db.cerrar_caso(thread_id, fila.get("decision", ""), fila.get("comentario", ""),
                   cierre=f"ejecutivo:{decision}", cierre_comentario=comentario)
    return {
        "estado": "resuelto",
        "thread_id": thread_id,
        "decision": decision,
        "resultado": final.get("resultado", {}),
        "traza": final.get("traza", []),
        **_escribir_en_gmail(thread_id, final),
    }


# --------------------------------------------------------------------------- #
# Al cerrar: borrador de respuesta y etiqueta en Gmail
# --------------------------------------------------------------------------- #
def borradores_activos() -> bool:
    """Apagado por defecto: escribir en el buzon es un efecto externo y debe ser
    una decision explicita, no algo que pasa por tener credenciales a mano."""
    return os.getenv("CARE_BORRADOR_GMAIL", "").lower() in ("1", "true", "si")


def _escribir_en_gmail(thread_id: str, final: dict[str, Any]) -> dict[str, Any]:
    """Respuesta de cierre como borrador y, si el caso cerro, etiqueta en el hilo.

    Va aqui y no en un nodo a proposito: el grafo no debe saber que existe Gmail.
    Si manana el canal es WhatsApp o un ticket de Zendesk, cambia esta funcion y
    ni los nodos ni las pruebas de topologia se enteran.

    Nunca hace fallar el cierre: el caso ya se resolvio y se registro. Un fallo
    aqui deja al ejecutivo sin borrador, no sin caso.
    """
    borrador, etiqueta = _borrador_al_cliente(thread_id, final)
    db.registrar_borrador(thread_id, borrador["estado"], borrador.get("draft_id", "") or "")
    db.registrar_etiqueta(thread_id, etiqueta)
    return {"borrador": borrador, "etiqueta": etiqueta}


def _borrador_al_cliente(thread_id: str, final: dict[str, Any]) -> tuple[dict[str, Any], str]:
    import langsmith

    from . import respuesta

    caso = final.get("caso", {})
    resultado = final.get("resultado", {})
    if not borradores_activos():
        return {"estado": "desactivado", "detalle": "CARE_BORRADOR_GMAIL no esta activo"}, ""

    correo = final.get("correo", {})
    try:
        # El orden importa: redactar antes de abrir el cliente de Gmail, para que
        # un fallo del modelo no llegue a tocar el buzon.
        acciones = respuesta.acciones_de_correo(resultado.get("acciones", []))
        # Corre fuera del grafo: sin este run raiz, LangSmith la muestra suelta
        # como "RunnableSequence" (el `modelo | parser` de with_structured_output).
        t = _traza(thread_id, "3 respuesta", caso.get("asunto", "") or correo.get("asunto", ""))
        with langsmith.trace(t["run_name"], tags=t["tags"], metadata=t["metadata"],
                             inputs={"caso_id": caso.get("caso_id", ""), "acciones": acciones}) as run:
            texto = respuesta.redactar(caso, acciones, correo.get("cuerpo", ""), resultado=resultado)
            run.end(outputs={"asunto": texto.asunto, "cuerpo": texto.cuerpo})
        from ..poller import gmail_ingesta

        svc = gmail_ingesta.cliente()
        creado = gmail_ingesta.crear_borrador(svc, correo, texto.asunto, texto.cuerpo)
    # SystemExit tambien: `gmail_ingesta.cliente()` lo usa para "no hay token",
    # pensado para la linea de comandos. Aqui no debe tumbar la UI.
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - ver docstring
        return {"estado": "error", "detalle": str(exc)}, ""

    borrador = {"estado": "creado", "draft_id": creado.get("id"),
                "asunto": texto.asunto, "cuerpo": texto.cuerpo}

    etiqueta = ""
    if resultado.get("cierre", {}).get("estado") == "cerrado" and correo.get("thread_id"):
        try:
            etiqueta = gmail_ingesta.etiquetar_hilo(svc, correo["thread_id"])
        except Exception as exc:  # noqa: BLE001
            etiqueta = f"error: {exc}"
    return borrador, etiqueta
