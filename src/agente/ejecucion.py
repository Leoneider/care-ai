"""Fase 2: ejecutar lo aprobado, verificar el resultado y decidir si el caso cierra solo.

Hasta la Fase 1 `aplicar_decision` anotaba cada accion como "simulada". Aqui cada
codigo del catalogo se traduce a una herramienta que opera sobre los sistemas, a
traves de `backends.py` y nunca directo.

Estados de una accion ejecutada:

- `ejecutada`: cambio algo en un sistema, y se puede verificar.
- `consultada`: solo leyo; informa, no cambia nada.
- `derivada`: abrio un ticket para otro equipo. El caso no puede cerrarse solo.
- `pendiente_cliente`: hace falta un dato del cliente. Tampoco cierra solo.
- `manual`: no hay herramienta automatica; la gestiona el ejecutivo.
- `error`: la herramienta fallo o el sistema rechazo la operacion.

Principio: los `parametros` que propone el modelo son pistas, no ordenes. Cuanto
ancho de banda aplicar sale de los datos extraidos y validados; cuanto acreditar
sale de facturacion y contratos. Nunca de lo que el modelo escribio.
"""

import os
from dataclasses import dataclass
from typing import Any, Callable

from . import backends, validaciones
from .prompts import CATALOGO_ACCIONES

EJECUTADA = "ejecutada"
CONSULTADA = "consultada"
DERIVADA = "derivada"
PENDIENTE_CLIENTE = "pendiente_cliente"
MANUAL = "manual"
ERROR = "error"

# Lo unico que puede cerrar un caso sin mirada humana.
SIN_RIESGO = (EJECUTADA, CONSULTADA)

TEMA_POR_TIPO = {
    "ampliacion_ancho_banda": "red",
    "falla_tecnica": "red",
    "facturacion_inconsistencia": "facturacion",
    "cambio_datos_contacto": "crm",
    "baja_servicio": "crm",
    "consulta_general": "crm",
    "otro": "crm",
}


def tope_nota_credito() -> int:
    """Por encima de esto una nota credito no se da por buena sin que la mire el
    ejecutivo, aunque se haya emitido y verificado. Es el ejemplo mas claro de
    "caso de riesgo": el sistema lo permite, el negocio quiere un par de ojos."""
    try:
        return int(os.getenv("CARE_TOPE_NOTA_CREDITO", "2000000"))
    except ValueError:
        return 2_000_000


@dataclass
class Contexto:
    caso_id: str
    cuenta_id: str
    cliente: dict[str, Any]
    solicitud: dict[str, Any]
    ticket: str


def _res(estado: str, detalle: str, **extra: Any) -> dict[str, Any]:
    return {"estado": estado, "detalle": detalle, **extra}


# --------------------------------------------------------------------------- #
# Herramientas
# --------------------------------------------------------------------------- #
def _servicio_y_objetivo(ctx: Contexto) -> tuple[dict[str, Any] | None, int]:
    datos = ctx.solicitud.get("datos", {})
    return (validaciones.buscar_servicio(ctx.cliente, datos.get("sede", "")),
            validaciones.numero(datos.get("ancho_banda_objetivo_mbps")))


def _factibilidad(ctx: Contexto) -> dict[str, Any]:
    servicio, objetivo = _servicio_y_objetivo(ctx)
    if not servicio:
        return _res(ERROR, "No se identifico el servicio de la sede indicada.")
    actual = backends.red_obtener_servicio(servicio["servicio_id"]) or servicio
    cap = backends.red_consultar_capacidad(actual["nodo"])
    delta = objetivo - actual["ancho_banda_mbps"]
    return _res(CONSULTADA, f"{actual['servicio_id']} en {actual['nodo']}: {cap['capacidad_libre_mbps']} Mbps libres "
                f"({cap['estado']}) para un aumento de {delta} Mbps.")


def _upgrade(ctx: Contexto) -> dict[str, Any]:
    servicio, objetivo = _servicio_y_objetivo(ctx)
    if not servicio:
        return _res(ERROR, "No se identifico el servicio de la sede indicada.")
    r = backends.red_aplicar_ancho_banda(servicio["servicio_id"], objetivo, ctx.caso_id)
    return _res(
        EJECUTADA,
        f"{r['servicio_id']} ({r['sede']}): {r['antes_mbps']} -> {r['despues_mbps']} Mbps. "
        f"El nodo {r['nodo']} queda con {r['capacidad_libre_nodo']} Mbps libres.",
        verificar={"tipo": "ancho_banda", "servicio_id": r["servicio_id"], "esperado": r["despues_mbps"]},
        referencia=r["servicio_id"],
    )


def _cotizar(ctx: Contexto) -> dict[str, Any]:
    """Cotizar solo hace falta si el contrato no cubre el upgrade.

    El modelo tiende a proponer COTIZAR_UPGRADE junto a APLICAR_UPGRADE_ANCHO_BANDA
    "por si acaso". Quien decide si hace falta es el contrato, no el modelo: si
    permite el upgrade sin adendum no hay nada que cotizar aparte; si no, la
    cotizacion y el adendum son trabajo del ejecutivo.
    """
    contrato = backends.contratos_obtener(ctx.cuenta_id)
    if contrato and contrato.get("permite_upgrade_sin_adendum"):
        return _res(CONSULTADA, f"No requiere cotizacion aparte: {contrato['contrato']} permite el upgrade sin adendum.")
    return _res(MANUAL, "El contrato exige adendum: la cotizacion y el adendum los gestiona el ejecutivo.")


def _factura_y_contrato(ctx: Contexto) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    return (validaciones.buscar_factura(ctx.cuenta_id, ctx.solicitud.get("datos", {})),
            backends.contratos_obtener(ctx.cuenta_id))


def _comparar(ctx: Contexto) -> dict[str, Any]:
    factura, contrato = _factura_y_contrato(ctx)
    if not factura or not contrato:
        return _res(ERROR, "No se encontro la factura o el contrato para comparar.")
    diff = factura["valor_neto"] - contrato["valor_mensual_pactado"]
    return _res(CONSULTADA, f"{factura['factura']}: neto {factura['valor_neto']:,} COP frente a "
                f"{contrato['valor_mensual_pactado']:,} COP pactados en {contrato['contrato']} "
                f"(diferencia {diff:,} COP).")


def _nota_credito(ctx: Contexto) -> dict[str, Any]:
    factura, contrato = _factura_y_contrato(ctx)
    if not factura or not contrato:
        return _res(ERROR, "No se encontro la factura o el contrato del reclamo.")
    pactado = contrato["valor_mensual_pactado"]
    diff = factura["valor_neto"] - pactado
    if diff <= 0:
        return _res(ERROR, f"{factura['factura']} ya coincide con lo pactado ({pactado:,} COP): no hay nada que acreditar.")
    nota = backends.facturacion_emitir_nota_credito(
        factura["factura"], diff, f"Diferencia frente a {contrato['contrato']} ({ctx.caso_id})", ctx.caso_id)
    return _res(
        EJECUTADA,
        f"{nota['nota']} por {nota['valor']:,} COP contra {nota['factura']}. "
        f"Neto {nota['neto_antes']:,} -> {nota['neto_despues']:,} COP, igual a lo pactado.",
        verificar={"tipo": "factura_neto", "factura": nota["factura"], "esperado": pactado},
        monto=nota["valor"], referencia=nota["nota"],
    )


def _asociar_ticket(ctx: Contexto) -> dict[str, Any]:
    tema = TEMA_POR_TIPO.get(ctx.solicitud.get("tipo", ""), "crm")
    previos = [t for t in backends.tickets_abiertos(ctx.cuenta_id, tema=tema)
               if t["ticket"] != ctx.ticket and t["caso_id"] != ctx.caso_id]
    if not previos:
        # No es un fallo: no habia nada abierto. El seguimiento queda en el ticket propio.
        return _res(CONSULTADA, f"No habia un ticket de {tema} abierto: el seguimiento queda en {ctx.ticket}.")
    previo = previos[0]["ticket"]
    backends.tickets_actualizar(previo, nota=f"Asociado al caso {ctx.caso_id} (seguimiento en {ctx.ticket}).",
                                caso_id=ctx.caso_id)
    return _res(EJECUTADA, f"Caso asociado a {previo}, que ya estaba abierto por {tema}.",
                verificar={"tipo": "nota_ticket", "ticket": previo, "contiene": ctx.caso_id},
                referencia=previo)


def _derivar(grupo: str, tema: str | None = None) -> Callable[[Contexto], dict[str, Any]]:
    def herramienta(ctx: Contexto) -> dict[str, Any]:
        t = tema or TEMA_POR_TIPO.get(ctx.solicitud.get("tipo", ""), "crm")
        numero = backends.tickets_crear(ctx.cuenta_id, t, f"[{ctx.caso_id}] {ctx.solicitud.get('resumen', '')}",
                                        grupo=grupo, estado="abierto", caso_id=ctx.caso_id)
        return _res(DERIVADA, f"Ticket {numero} abierto para {grupo}.", referencia=numero, grupo=grupo)
    return herramienta


def _pedir_dato(ctx: Contexto) -> dict[str, Any]:
    faltan = ", ".join(ctx.solicitud.get("faltantes", [])) or "el dato que falta"
    return _res(PENDIENTE_CLIENTE, f"Se le pide al cliente: {faltan}. Va en la respuesta de cierre.")


HERRAMIENTAS: dict[str, Callable[[Contexto], dict[str, Any]]] = {
    "VERIFICAR_FACTIBILIDAD_TECNICA": _factibilidad,
    "APLICAR_UPGRADE_ANCHO_BANDA": _upgrade,
    "COTIZAR_UPGRADE": _cotizar,
    "COMPARAR_FACTURA_VS_CONTRATO": _comparar,
    "SOLICITAR_NOTA_CREDITO": _nota_credito,
    "ASOCIAR_A_TICKET_EXISTENTE": _asociar_ticket,
    "SOLICITAR_DATO_FALTANTE": _pedir_dato,
    "ESCALAR_A_INGENIERIA": _derivar("Ingenieria"),
    "ESCALAR_A_FACTURACION": _derivar("Facturacion"),
    "ESCALAR_A_NOC": _derivar("NOC"),
    "ESCALAR_A_RETENCION": _derivar("Retencion"),
    "ESCALAR_A_SUPERVISOR": _derivar("Supervisor"),
    "AGENDAR_VISITA_TECNICA": _derivar("Campo"),
    "CREAR_INCIDENTE": _derivar("NOC", "red"),
}


# --------------------------------------------------------------------------- #
# Ejecucion
# --------------------------------------------------------------------------- #
def repartir(caso: dict[str, Any], finales: list[dict[str, Any]]) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """Asigna cada accion final a su solicitud.

    `acciones_finales` llega plana (la UI no manda el id de la solicitud), pero
    es una subsecuencia de lo propuesto: la UI solo quita acciones o edita su
    texto, nunca las reordena ni cambia su codigo. Se recorren las propuestas en
    orden y cada final se asigna a la primera propuesta con su mismo codigo.
    Lo que no encaje (un llamador que no sea la UI) va a la solicitud cuyo
    catalogo permite ese codigo.
    """
    sols = caso.get("solicitudes", [])
    reparto: dict[str, list[dict[str, Any]]] = {s["id"]: [] for s in sols}
    pendientes = list(finales)
    for s in sols:
        for propuesta in s.get("acciones", []):
            if pendientes and pendientes[0].get("codigo") == propuesta.get("codigo"):
                reparto[s["id"]].append(pendientes.pop(0))
    for a in pendientes:
        destino = next((s for s in sols if a.get("codigo") in CATALOGO_ACCIONES.get(s["tipo"], [])),
                       sols[0] if sols else None)
        if destino:
            reparto[destino["id"]].append(a)
    return [(s, reparto[s["id"]]) for s in sols if reparto[s["id"]]]


def ejecutar(caso: dict[str, Any], cliente: dict[str, Any], finales: list[dict[str, Any]]) -> dict[str, Any]:
    """Corre las acciones aprobadas y deja un ticket de seguimiento por solicitud."""
    caso_id = caso.get("caso_id", "")
    cuenta_id = cliente.get("cuenta_id", "")
    acciones: list[dict[str, Any]] = []
    tickets: list[dict[str, Any]] = []

    for sol, suyas in repartir(caso, finales):
        ticket = ""
        if cuenta_id:
            tema = TEMA_POR_TIPO.get(sol["tipo"], "crm")
            ticket = backends.tickets_crear(cuenta_id, tema, f"[{caso_id}] {sol.get('resumen', '')}",
                                            grupo="Care", estado="en_proceso", caso_id=caso_id)
            tickets.append({"ticket": ticket, "solicitud": sol["id"], "tema": tema})

        ctx = Contexto(caso_id=caso_id, cuenta_id=cuenta_id, cliente=cliente, solicitud=sol, ticket=ticket)
        for accion in suyas:
            herramienta = HERRAMIENTAS.get(accion["codigo"])
            if not cuenta_id:
                r = _res(MANUAL, "Cliente no identificado en el CRM: no hay sobre que ejecutar.")
            elif herramienta is None:
                r = _res(MANUAL, "Sin herramienta automatica en la POC: la gestiona el ejecutivo.")
            else:
                try:
                    r = herramienta(ctx)
                except backends.ErrorSistema as exc:
                    r = _res(ERROR, f"El sistema rechazo la operacion: {exc}")
                except Exception as exc:  # noqa: BLE001 - una herramienta rota no tumba el caso
                    r = _res(ERROR, f"Fallo inesperado: {exc}")
            acciones.append({"codigo": accion["codigo"], "sistema": accion.get("sistema", ""),
                             "solicitud": sol["id"], **r})
            if ticket:
                backends.tickets_actualizar(ticket, nota=f"{accion['codigo']}: {r['estado']}. {r['detalle']}",
                                            caso_id=caso_id)

    return {"estado": "ejecutado", "acciones": acciones, "tickets": tickets}


# --------------------------------------------------------------------------- #
# Verificacion
# --------------------------------------------------------------------------- #
def _comprobar(v: dict[str, Any]) -> tuple[bool, str]:
    if v["tipo"] == "ancho_banda":
        s = backends.red_obtener_servicio(v["servicio_id"])
        real = s["ancho_banda_mbps"] if s else None
        return real == v["esperado"], f"{v['servicio_id']} tiene {real} Mbps (esperado {v['esperado']})."
    if v["tipo"] == "factura_neto":
        f = backends.facturacion_obtener_factura(v["factura"])
        real = f["valor_neto"] if f else None
        return real == v["esperado"], f"{v['factura']} queda en {real:,} COP netos (esperado {v['esperado']:,})." \
            if real is not None else f"{v['factura']} no existe."
    if v["tipo"] == "nota_ticket":
        t = backends.tickets_obtener(v["ticket"])
        ok = bool(t) and any(v["contiene"] in n["texto"] for n in t.get("notas", []))
        return ok, f"{v['ticket']} {'registra' if ok else 'no registra'} la asociacion."
    return False, f"No se sabe verificar '{v['tipo']}'."


def verificar(resultado: dict[str, Any]) -> list[dict[str, Any]]:
    """Vuelve a leer los sistemas: que la herramienta dijera "ok" no basta."""
    checks = []
    for a in resultado.get("acciones", []):
        if a.get("estado") == EJECUTADA and a.get("verificar"):
            try:
                ok, detalle = _comprobar(a["verificar"])
            except Exception as exc:  # noqa: BLE001
                ok, detalle = False, f"No se pudo verificar: {exc}"
            checks.append({"codigo": a["codigo"], "solicitud": a["solicitud"], "ok": ok, "detalle": detalle})
    return checks


def evaluar_cierre(caso: dict[str, Any], resultado: dict[str, Any], checks: list[dict[str, Any]]) -> list[str]:
    """Motivos para que el ejecutivo mire antes de cerrar. Vacio = cierra solo."""
    motivos: list[str] = []
    for a in resultado.get("acciones", []):
        estado = a.get("estado")
        if estado == ERROR:
            motivos.append(f"{a['codigo']} fallo: {a['detalle']}")
        elif estado == DERIVADA:
            motivos.append(f"{a['codigo']} quedo en manos de {a.get('grupo', 'otro equipo')}.")
        elif estado == PENDIENTE_CLIENTE:
            motivos.append(f"{a['codigo']}: falta un dato del cliente.")
        elif estado == MANUAL:
            motivos.append(f"{a['codigo']}: {a['detalle']}")

    motivos += [f"Verificacion fallida: {c['detalle']}" for c in checks if not c["ok"]]

    tope = tope_nota_credito()
    motivos += [
        f"La nota credito de {a['monto']:,} COP supera el tope de {tope:,} COP para cerrar sin revision."
        for a in resultado.get("acciones", [])
        if a.get("estado") == EJECUTADA and a.get("monto", 0) > tope
    ]

    if any(v.get("bloqueante") for s in caso.get("solicitudes", []) for v in s.get("validaciones", [])):
        motivos.append("Se aprobo pese a una validacion bloqueante.")

    if not caso.get("cuenta") or caso.get("cuenta") == "NO IDENTIFICADA":
        motivos.append("El remitente no esta asociado a ninguna cuenta.")
    return motivos


# --------------------------------------------------------------------------- #
# Cierre
# --------------------------------------------------------------------------- #
def cerrar(resultado: dict[str, Any], *, caso_id: str, decision: str, modo: str, comentario: str = "") -> dict[str, Any]:
    """Cierra (o deja en espera) los tickets de seguimiento del caso.

    Los tickets derivados a otros equipos no se tocan: son de ellos. Solo se cierran
    los que abrio Care para seguir el caso.
    """
    cerrar_todo = decision == "cerrar"
    estado_ticket = "resuelto" if cerrar_todo else "en_espera"
    nota = (f"Cierre {modo}: " + ("caso resuelto." if cerrar_todo else "queda en espera.")
            + (f" {comentario}" if comentario else ""))
    finales = []
    for t in resultado.get("tickets", []):
        try:
            backends.tickets_actualizar(t["ticket"], estado=estado_ticket, nota=nota, caso_id=caso_id)
            finales.append({**t, "estado": estado_ticket})
        except Exception as exc:  # noqa: BLE001
            finales.append({**t, "estado": "error", "detalle": str(exc)})
    return {"modo": modo, "estado": "cerrado" if cerrar_todo else "abierto",
            "decision": decision, "comentario": comentario, "tickets": finales}
