"""Nodos del grafo.

Cada nodo hace una sola cosa y devuelve un update parcial del estado.
Esa disciplina es la que permite despues: reintentar un nodo, cachear,
medirlo por separado y reemplazar el LLM por reglas donde convenga.
"""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langgraph.types import interrupt

from . import backends, db, ejecucion, validaciones
from .llm import get_structured_llm
from .prompts import (
    ACCIONES_SYSTEM,
    CATALOGO_ACCIONES,
    DATOS_REQUERIDOS,
    EXTRACCION_SYSTEM,
    SEGMENTACION_SYSTEM,
    SISTEMA_POR_ACCION,
)
from .schemas import (
    DatosExtraidos,
    PlanAcciones,
    Segmentacion,
    SolicitudDetectada,
    SolicitudProcesada,
)
from .state import CareState, SolicitudState

UMBRAL_CONFIANZA = 0.75


# --------------------------------------------------------------------------- #
# 1. Ingesta
# --------------------------------------------------------------------------- #
CITA_PREVIA = re.compile(r"(^>.*$)|(^-{2,}\s*mensaje original.*$)", re.IGNORECASE | re.MULTILINE)
FIRMA = re.compile(r"\n--\s*\n.*", re.DOTALL)


def ingesta(state: CareState) -> dict[str, Any]:
    """Normaliza el correo y corta si ya lo procesamos.

    La idempotencia va primero: un buzon reintenta, un webhook se duplica,
    y no queremos dos casos para el mismo Message-ID.
    """
    correo = dict(state["correo"])

    if backends.ya_procesado(correo.get("message_id", "")):
        return {"duplicado": True, "traza": ["ingesta: correo duplicado, se descarta"]}

    cuerpo = correo.get("cuerpo", "")
    cuerpo = CITA_PREVIA.sub("", cuerpo)
    cuerpo = FIRMA.sub("", cuerpo)
    correo["cuerpo"] = cuerpo.strip()
    correo.setdefault("recibido_en", datetime.now(timezone.utc).isoformat())

    return {"correo": correo, "duplicado": False, "traza": ["ingesta: correo normalizado"]}


# --------------------------------------------------------------------------- #
# 2. Identificacion del cliente
# --------------------------------------------------------------------------- #
def identificar_cliente(state: CareState) -> dict[str, Any]:
    cliente = backends.crm_buscar_cuenta_por_correo(state["correo"]["remitente"])
    if not cliente.get("encontrado"):
        return {
            "cliente": cliente,
            "traza": ["cliente: remitente no asociado a ninguna cuenta B2B"],
        }
    nota = "" if cliente["contacto_autorizado"] else " (contacto NO autorizado en el CRM)"
    return {
        "cliente": cliente,
        "traza": [f"cliente: {cliente['razon_social']} / {cliente['cuenta_id']}{nota}"],
    }


# --------------------------------------------------------------------------- #
# 3. Segmentacion en solicitudes
# --------------------------------------------------------------------------- #
def segmentar(state: CareState) -> dict[str, Any]:
    llm = get_structured_llm(Segmentacion)
    correo = state["correo"]
    entrada = (
        f"Asunto: {correo.get('asunto','')}\n"
        f"De: {correo.get('remitente','')}\n\n"
        f"{correo.get('cuerpo','')}"
    )
    res: Segmentacion = llm.invoke(
        [{"role": "system", "content": SEGMENTACION_SYSTEM}, {"role": "user", "content": entrada}]
    )
    return {
        "solicitudes_detectadas": [s.model_dump() for s in res.solicitudes],
        "traza": [f"segmentacion: {len(res.solicitudes)} solicitud(es) detectada(s)"],
    }


# --------------------------------------------------------------------------- #
# 4. Rama paralela por solicitud: extraer -> validar -> proponer acciones
# --------------------------------------------------------------------------- #
def procesar_solicitud(state: SolicitudState) -> dict[str, Any]:
    sol = SolicitudDetectada(**state["solicitud"])
    cliente = state["cliente"]
    correo = state["correo"]

    # 4a. extraccion de datos
    llm_datos = get_structured_llm(DatosExtraidos)
    requeridos = DATOS_REQUERIDOS.get(sol.tipo, [])
    datos: DatosExtraidos = llm_datos.invoke(
        [
            {"role": "system", "content": EXTRACCION_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"Tipo de solicitud: {sol.tipo}\n"
                    f"Datos requeridos: {', '.join(requeridos) or 'ninguno'}\n"
                    f"Fragmento relevante: {sol.texto_fuente}\n\n"
                    f"Correo completo:\n{correo.get('cuerpo','')}"
                ),
            },
        ]
    )
    faltantes = sorted(set(datos.faltantes) | {r for r in requeridos if r not in datos.campos})

    # 4b. validacion determinista contra sistemas
    checks = validaciones.validar(sol.tipo, cliente, datos.campos)

    # 4c. plan de acciones, restringido al catalogo
    catalogo = CATALOGO_ACCIONES.get(sol.tipo, CATALOGO_ACCIONES["otro"])
    llm_acc = get_structured_llm(PlanAcciones)
    plan: PlanAcciones = llm_acc.invoke(
        [
            {"role": "system", "content": ACCIONES_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"Tipo: {sol.tipo}\n"
                    f"Catalogo permitido: {catalogo}\n"
                    f"Datos extraidos: {json.dumps(datos.campos, ensure_ascii=False)}\n"
                    f"Datos faltantes: {faltantes}\n"
                    f"Validaciones: {json.dumps([c.model_dump() for c in checks], ensure_ascii=False)}\n"
                    f"Cliente: {cliente.get('razon_social','desconocido')} "
                    f"({cliente.get('segmento','-')})"
                ),
            },
        ]
    )

    # Guardarrail: descartar acciones fuera del catalogo
    fuera = [a.codigo for a in plan.acciones if a.codigo not in catalogo]
    # El sistema sale del catalogo, no del modelo (ver SISTEMA_POR_ACCION).
    acciones = [
        a.model_copy(update={"sistema": SISTEMA_POR_ACCION.get(a.codigo, a.sistema)})
        for a in plan.acciones
        if a.codigo in catalogo
    ]

    # 4d. confianza y bandera de atencion
    bloqueantes = [c for c in checks if c.bloqueante]
    fallas = [c for c in checks if c.resultado == "falla"]
    confianza = sol.confianza
    if fallas:
        confianza *= 0.6
    if faltantes:
        confianza *= 0.85
    if not cliente.get("encontrado") or not cliente.get("contacto_autorizado", True):
        confianza *= 0.7

    motivos = []
    if bloqueantes:
        motivos.append("validacion bloqueante: " + bloqueantes[0].detalle)
    if faltantes:
        motivos.append("faltan datos: " + ", ".join(faltantes))
    if fuera:
        motivos.append("el modelo propuso acciones fuera de catalogo: " + ", ".join(fuera))
    if confianza < UMBRAL_CONFIANZA:
        motivos.append(f"confianza {confianza:.2f} por debajo del umbral {UMBRAL_CONFIANZA}")

    procesada = SolicitudProcesada(
        id=sol.id,
        tipo=sol.tipo,
        resumen=sol.resumen,
        texto_fuente=sol.texto_fuente,
        datos=datos.campos,
        faltantes=faltantes,
        validaciones=checks,
        acciones=acciones,
        confianza=round(confianza, 2),
        requiere_atencion=bool(motivos),
        motivo_atencion=" | ".join(motivos),
    )
    return {"solicitudes": [procesada.model_dump()], "traza": [f"solicitud {sol.id} ({sol.tipo}) procesada"]}


# --------------------------------------------------------------------------- #
# 5. Ensamblado del caso
# --------------------------------------------------------------------------- #
PRIORIDAD_POR_TIPO = {
    "falla_tecnica": 1,
    "facturacion_inconsistencia": 2,
    "baja_servicio": 2,
    "ampliacion_ancho_banda": 3,
    "cambio_datos_contacto": 4,
    "consulta_general": 5,
    "otro": 3,
}


def ensamblar_caso(state: CareState) -> dict[str, Any]:
    sols = sorted(state.get("solicitudes", []), key=lambda s: PRIORIDAD_POR_TIPO.get(s["tipo"], 3))
    cliente = state.get("cliente", {})
    atencion = [s for s in sols if s["requiere_atencion"]]

    caso = {
        "caso_id": "CASE-" + re.sub(r"[^A-Za-z0-9]", "", state["correo"].get("message_id", "sinid"))[:14].upper(),
        "recibido_en": state["correo"].get("recibido_en"),
        "cuenta": cliente.get("cuenta_id", "NO IDENTIFICADA"),
        "razon_social": cliente.get("razon_social", "-"),
        "asunto": state["correo"].get("asunto", ""),
        "prioridad": min((PRIORIDAD_POR_TIPO.get(s["tipo"], 3) for s in sols), default=3),
        "solicitudes": sols,
        "confianza_global": round(min((s["confianza"] for s in sols), default=0.0), 2),
        "requiere_atencion": bool(atencion) or not cliente.get("encontrado", False),
        "resumen_ejecutivo": " ".join(f"[{s['tipo']}] {s['resumen']}" for s in sols),
    }
    return {"caso": caso, "traza": ["caso ensamblado"]}


# --------------------------------------------------------------------------- #
# 6. Revision humana (Fase 1: SIEMPRE pasa por aqui)
# --------------------------------------------------------------------------- #
def revision_humana(state: CareState) -> dict[str, Any]:
    """Pausa el grafo y entrega el caso al ejecutivo Care.

    `interrupt` congela el estado en el checkpointer. El grafo se reanuda con
    `Command(resume=...)` cuando el ejecutivo decide, incluso dias despues y
    desde otro proceso. Eso es lo que convierte esto en un copiloto real y no
    en un script que corre de un tiron.
    """
    decision = interrupt(
        {
            "tipo": "revision_care",
            "caso": state["caso"],
            "opciones": ["aprobar", "editar", "rechazar"],
            "instruccion": "Revise las acciones propuestas y apruebe, edite o rechace el caso.",
        }
    )
    return {"decision_humana": decision, "traza": [f"revision humana: {decision.get('decision')}"]}


# --------------------------------------------------------------------------- #
# 7. Ejecucion posterior + captura de feedback
# --------------------------------------------------------------------------- #
def ruta_feedback() -> Path:
    """Donde se acumula el dataset de revisiones.

    Relativa a la raiz del proyecto y no al CWD: antes, lanzar el demo desde otra
    carpeta creaba un feedback.jsonl distinto y `pytest` escribia en el mismo
    archivo que las revisiones reales. Ahora el conftest apunta
    CARE_FEEDBACK_JSONL a un temporal y las pruebas no tocan el dataset.

    Sigue siendo un .jsonl: `src/agente/feedback.py` (la version en BD) esta escrito
    pero deliberadamente sin conectar, y esa decision no cambia aqui.
    """
    valor = Path(os.getenv("CARE_FEEDBACK_JSONL", "feedback.jsonl"))
    return valor if valor.is_absolute() else db.RAIZ / valor


def aplicar_decision(state: CareState) -> dict[str, Any]:
    """Fase 2: ejecuta en los sistemas lo que el ejecutivo aprobo.

    Antes cada accion se anotaba como "simulada". Ahora `ejecucion.ejecutar` la
    traduce a una herramienta que escribe en el mini sistema legado, y abre un
    ticket de seguimiento por solicitud.
    """
    decision = state.get("decision_humana", {})
    caso = state.get("caso", {})
    tipo = decision.get("decision")

    if tipo == "rechazar":
        resultado = {"estado": "descartado", "motivo": decision.get("comentario", "")}
    else:
        acciones_finales = decision.get("acciones_finales") or [
            a for s in caso.get("solicitudes", []) for a in s["acciones"]
        ]
        resultado = ejecucion.ejecutar(caso, state.get("cliente", {}), acciones_finales)

    backends.marcar_procesado(state["correo"].get("message_id", ""))

    # Feedback para el dataset de evaluacion: la edicion del humano es la etiqueta
    # de oro. Guardarla desde el dia 1 es lo que permite medir mejora en Fase 2.
    feedback = {
        "caso_id": caso.get("caso_id"),
        "decision": tipo,
        "comentario": decision.get("comentario", ""),
        "propuesto": caso.get("solicitudes", []),
        "final": decision.get("acciones_finales"),
    }
    ruta_feedback().parent.mkdir(parents=True, exist_ok=True)
    with open(ruta_feedback(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(feedback, ensure_ascii=False, default=str) + "\n")

    if resultado["estado"] == "descartado":
        return {"resultado": resultado, "traza": ["decision aplicada: descartado"]}
    hechas = sum(a["estado"] in ejecucion.SIN_RIESGO for a in resultado["acciones"])
    return {
        "resultado": resultado,
        "traza": [f"ejecucion: {hechas}/{len(resultado['acciones'])} accion(es) sin intervencion, "
                  f"{len(resultado['tickets'])} ticket(s) de seguimiento"],
    }


# --------------------------------------------------------------------------- #
# 8. Fase 2: verificacion, revision de cierre y cierre
# --------------------------------------------------------------------------- #
def verificar_resultado(state: CareState) -> dict[str, Any]:
    """Vuelve a leer los sistemas y decide si el caso puede cerrarse solo."""
    resultado = state.get("resultado", {})
    checks = ejecucion.verificar(resultado)
    motivos = ejecucion.evaluar_cierre(state.get("caso", {}), resultado, checks)
    ok = sum(c["ok"] for c in checks)
    return {
        "verificacion": {"checks": checks, "motivos": motivos, "automatico": not motivos},
        "traza": [f"verificacion: {ok}/{len(checks)} cambio(s) confirmados; "
                  + ("cierre automatico" if not motivos else f"{len(motivos)} motivo(s) para revisar")],
    }


def revision_cierre(state: CareState) -> dict[str, Any]:
    """Segundo punto humano (Human in the Loop de la Fase 2).

    Solo llegan aqui los casos de riesgo: algo fallo, quedo derivado, falta un dato
    del cliente, o el monto supera el tope. El ejecutivo ve lo que se hizo y lo
    que se verifico, y decide si cierra o deja el ticket abierto.
    """
    decision = interrupt(
        {
            "tipo": "revision_cierre",
            "caso": state["caso"],
            "resultado": state.get("resultado", {}),
            "verificacion": state.get("verificacion", {}),
            "opciones": ["cerrar", "dejar_abierto"],
            "instruccion": "Revise lo ejecutado y verificado. Cierre el caso o deje el ticket abierto.",
        }
    )
    return {"decision_cierre": decision, "traza": [f"revision de cierre: {decision.get('decision')}"]}


def cerrar_caso(state: CareState) -> dict[str, Any]:
    humano = state.get("decision_cierre")
    modo = "ejecutivo" if humano else "automatico"
    decision = (humano or {}).get("decision", "cerrar")
    cierre = ejecucion.cerrar(
        state.get("resultado", {}),
        caso_id=state.get("caso", {}).get("caso_id", ""),
        decision=decision,
        modo=modo,
        comentario=(humano or {}).get("comentario", ""),
    )
    return {
        "resultado": {**state.get("resultado", {}), "cierre": cierre},
        "traza": [f"cierre {modo}: caso {cierre['estado']}"],
    }
