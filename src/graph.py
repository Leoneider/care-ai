"""Topologia del grafo.

Fase 1:
correo -> ingesta -> cliente -> segmentar -=[fan-out]=> procesar_solicitud
       -> ensamblar_caso -> [INTERRUPT: revision humana]

Fase 2:
       -> aplicar_decision (ejecuta en los sistemas) -> verificar_resultado
       -> sin riesgo: cerrar_caso
       -> con riesgo: [INTERRUPT: revision de cierre] -> cerrar_caso
"""

import os
import sqlite3
import threading

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from . import db
from .nodes import (
    aplicar_decision,
    cerrar_caso,
    ensamblar_caso,
    identificar_cliente,
    ingesta,
    procesar_solicitud,
    revision_cierre,
    revision_humana,
    segmentar,
    verificar_resultado,
)
from .state import CareState


def _ruta_despues_de_ingesta(state: CareState) -> str:
    return END if state.get("duplicado") else "identificar_cliente"


def _ruta_despues_de_decidir(state: CareState) -> str:
    """Un rechazo no ejecuta nada, asi que tampoco hay nada que verificar."""
    return END if state.get("resultado", {}).get("estado") == "descartado" else "verificar_resultado"


def _ruta_despues_de_verificar(state: CareState) -> str:
    return "cerrar_caso" if state.get("verificacion", {}).get("automatico") else "revision_cierre"


def _fan_out(state: CareState) -> list[Send] | str:
    """Una rama paralela por solicitud detectada.

    `Send` permite N ramas dinamicas: el numero de solicitudes no se conoce
    hasta que el modelo lee el correo. Cada rama escribe en `solicitudes`,
    que tiene un reducer aditivo.
    """
    detectadas = state.get("solicitudes_detectadas", [])
    if not detectadas:
        return "ensamblar_caso"
    return [
        Send(
            "procesar_solicitud",
            {"solicitud": s, "cliente": state.get("cliente", {}), "correo": state["correo"]},
        )
        for s in detectadas
    ]


_CHECKPOINTERS: dict[str, object] = {}
_LOCK = threading.Lock()


def checkpointer_por_defecto():
    """SqliteSaver sobre el mismo archivo que la bandeja.

    InMemorySaver alcanzaba mientras el demo corria de un tiron. Ya no: el correo
    entra por el poller de Gmail y la decision llega desde la UI, que es otro
    proceso. Un `interrupt` guardado en RAM no sobrevive a eso ni a un reinicio.

    `CARE_CHECKPOINT=memory` vuelve al saver en memoria, que es lo que queremos en
    los tests de topologia: no dejan archivo y cada corrida arranca limpia.
    """
    if os.getenv("CARE_CHECKPOINT", "sqlite").lower() == "memory":
        return InMemorySaver()

    from langgraph.checkpoint.sqlite import SqliteSaver

    ruta = str(db.ruta_db())
    with _LOCK:
        if ruta not in _CHECKPOINTERS:
            db.ruta_db().parent.mkdir(parents=True, exist_ok=True)
            # check_same_thread=False porque uvicorn atiende en varios threads;
            # SqliteSaver serializa sus escrituras con un lock propio.
            con = sqlite3.connect(ruta, check_same_thread=False, timeout=15)
            con.execute("PRAGMA journal_mode=WAL")
            _CHECKPOINTERS[ruta] = SqliteSaver(con)
        return _CHECKPOINTERS[ruta]


def topologia() -> StateGraph:
    """La topologia sin compilar.

    Separada de `construir_grafo` porque el servidor de LangGraph (`langgraph dev`,
    LangGraph Studio) aporta su propia persistencia y rechaza un grafo que ya venga
    compilado con checkpointer propio. Aqui devolvemos el builder; quien lo use
    decide con que saver lo compila.
    """
    g = StateGraph(CareState)

    g.add_node("ingesta", ingesta)
    g.add_node("identificar_cliente", identificar_cliente)
    g.add_node("segmentar", segmentar)
    g.add_node("procesar_solicitud", procesar_solicitud)
    g.add_node("ensamblar_caso", ensamblar_caso)
    g.add_node("revision_humana", revision_humana)
    g.add_node("aplicar_decision", aplicar_decision)
    g.add_node("verificar_resultado", verificar_resultado)
    g.add_node("revision_cierre", revision_cierre)
    g.add_node("cerrar_caso", cerrar_caso)

    g.add_edge(START, "ingesta")
    g.add_conditional_edges("ingesta", _ruta_despues_de_ingesta, ["identificar_cliente", END])
    g.add_edge("identificar_cliente", "segmentar")
    g.add_conditional_edges("segmentar", _fan_out, ["procesar_solicitud", "ensamblar_caso"])
    g.add_edge("procesar_solicitud", "ensamblar_caso")
    g.add_edge("ensamblar_caso", "revision_humana")
    g.add_edge("revision_humana", "aplicar_decision")
    g.add_conditional_edges("aplicar_decision", _ruta_despues_de_decidir, ["verificar_resultado", END])
    g.add_conditional_edges("verificar_resultado", _ruta_despues_de_verificar, ["cerrar_caso", "revision_cierre"])
    g.add_edge("revision_cierre", "cerrar_caso")
    g.add_edge("cerrar_caso", END)

    return g


def construir_grafo(checkpointer=None):
    # SqliteSaver: el caso sobrevive al reinicio y el ejecutivo puede revisarlo
    # horas despues, desde otro proceso. En piloto multi-instancia, PostgresSaver
    # con la misma firma. Falta la poda de checkpoints intermedios (~9 por correo,
    # ver README).
    return topologia().compile(checkpointer=checkpointer or checkpointer_por_defecto())
