"""Redaccion del borrador de respuesta al cliente.

Cierra el unico lazo que quedaba abierto: el catalogo de `prompts.py` incluye
acciones con `sistema: CORREO` — sobre todo `SOLICITAR_DATO_FALTANTE` — y hasta
ahora se marcaban como `simulada` y ahi morian. El plan decia "pedirle al cliente
la fecha de activacion" y nadie se la pedia.

**Borrador, no envio.** El texto se deja como borrador dentro del hilo de Gmail;
el ejecutivo lo abre, lo corrige y lo manda el. Eso conserva el principio de la
Fase 1 — el humano es el unico que envia — y evita el modo de fallo caro: un
correo automatico y equivocado a un cliente no se puede deshacer.

Por eso tampoco hace falta ampliar el scope: `gmail.modify`, que ya usamos para
etiquetar, alcanza para crear borradores. Enviar exigiria `gmail.send`, que
deliberadamente no pedimos.

La redaccion la hace el modelo, con el mismo patron que el resto: salida tipada
(`RespuestaCliente`) y un prompt que le prohibe prometer nada que no se haya hecho.

**Fase 2.** Con las acciones ejecutadas, el borrador pasa a ser la respuesta de
cierre: cuenta lo que se hizo y se verifico, con las cifras exactas que salen de
los sistemas (no del modelo), lo que quedo en curso y lo que falta del cliente.
"""

import json
from typing import Any

from .llm import get_structured_llm
from .prompts import RESPUESTA_CIERRE_SYSTEM, RESPUESTA_SYSTEM
from .schemas import RespuestaCliente

SISTEMA_CORREO = "CORREO"


def acciones_de_correo(acciones: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Las acciones aprobadas que implican escribirle al cliente."""
    return [a for a in acciones if (a.get("sistema") or "").upper() == SISTEMA_CORREO]


def _redactar_cierre(caso: dict[str, Any], resultado: dict[str, Any], cuerpo_original: str) -> RespuestaCliente:
    acciones = resultado.get("acciones", [])
    hechas = [f"- {a['detalle']}" for a in acciones if a.get("estado") == "ejecutada"]
    # Lo que solo se leyo (el plan actual, una comparacion) es justo lo que una
    # consulta espera de respuesta: sin esto el modelo acusaba recibo y nada mas.
    consultadas = [f"- {a['detalle']}" for a in acciones if a.get("estado") == "consultada"]
    en_curso = [f"- {a['codigo']}: {a['detalle']}" for a in acciones
                if a.get("estado") in ("derivada", "manual", "error")]
    pide_datos = any(a.get("estado") == "pendiente_cliente" for a in acciones)
    faltantes = sorted({f for s in caso.get("solicitudes", []) for f in s.get("faltantes", [])}) if pide_datos else []
    cierre = resultado.get("cierre", {})
    # Con el tema al lado: con solo los numeros, el modelo asigna cada ticket a la
    # solicitud que le parece y a veces los cruza.
    tickets = ", ".join(
        f"{t['ticket']} ({t['tema']})" if t.get("tema") else t["ticket"]
        for t in cierre.get("tickets", resultado.get("tickets", []))
    )

    contenido = "\n".join([
        f"Cliente: {caso.get('razon_social', '-')} ({caso.get('cuenta', '-')})",
        f"Asunto original: {caso.get('asunto', '')}",
        f"Estado del caso: {cierre.get('estado', 'cerrado')}",
        f"Tickets de seguimiento: {tickets or 'ninguno'}",
        "",
        "Hecho y verificado en los sistemas:",
        *(hechas or ["- nada"]),
        "",
        "Consultado en los sistemas (informar al cliente):",
        *(consultadas or ["- nada"]),
        "",
        "En curso:",
        *(en_curso or ["- nada"]),
        "",
        f"Datos que faltan del cliente: {', '.join(faltantes) or 'ninguno'}",
        "",
        "Correo original del cliente:",
        cuerpo_original,
    ])
    llm = get_structured_llm(RespuestaCliente)
    return llm.invoke(
        [
            {"role": "system", "content": RESPUESTA_CIERRE_SYSTEM},
            {"role": "user", "content": contenido},
        ]
    )


def redactar(
    caso: dict[str, Any],
    acciones: list[dict[str, Any]],
    cuerpo_original: str = "",
    resultado: dict[str, Any] | None = None,
) -> RespuestaCliente:
    """Redacta el borrador de respuesta al cliente.

    Con `resultado` (Fase 2) es la respuesta de cierre, y cuenta lo ejecutado. Sin
    el, redacta solo para las acciones de correo aprobadas, sin afirmar nada hecho.
    Recibe las acciones *finales*, no las propuestas: si el ejecutivo quito la de
    pedir la fecha, el borrador no debe pedirla.
    """
    if resultado and resultado.get("acciones") is not None:
        return _redactar_cierre(caso, resultado, cuerpo_original)

    faltantes = sorted(
        {f for s in caso.get("solicitudes", []) for f in s.get("faltantes", [])}
    )
    resumenes = [f"- {s['tipo']}: {s['resumen']}" for s in caso.get("solicitudes", [])]

    llm = get_structured_llm(RespuestaCliente)
    return llm.invoke(
        [
            {"role": "system", "content": RESPUESTA_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"Cliente: {caso.get('razon_social', '-')} ({caso.get('cuenta', '-')})\n"
                    f"Asunto original: {caso.get('asunto', '')}\n\n"
                    f"Solicitudes detectadas:\n" + "\n".join(resumenes) + "\n\n"
                    f"Datos que faltan: {', '.join(faltantes) or 'ninguno'}\n\n"
                    f"Acciones de correo aprobadas:\n"
                    + json.dumps(acciones, ensure_ascii=False, indent=2)
                    + f"\n\nCorreo original del cliente:\n{cuerpo_original}"
                ),
            },
        ]
    )
