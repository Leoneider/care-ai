"""Estado del grafo.

Dos esquemas:
- CareState: el estado global del caso (un correo = un thread = un checkpoint).
- SolicitudState: el payload que viaja en cada `Send` del fan-out por solicitud.
"""

import operator
from typing import Annotated, Any, TypedDict



class CorreoEntrante(TypedDict, total=False):
    message_id: str          # clave de idempotencia
    thread_id: str           # hilo de conversacion del cliente
    remitente: str
    para: list[str]
    asunto: str
    cuerpo: str              # ya limpio de firmas y citas previas
    recibido_en: str         # ISO 8601
    adjuntos: list[str]


class Cliente(TypedDict, total=False):
    encontrado: bool
    cuenta_id: str
    razon_social: str
    segmento: str
    estado: str
    contacto_autorizado: bool
    servicios: list[dict[str, Any]]


class CareState(TypedDict, total=False):
    correo: CorreoEntrante
    duplicado: bool
    cliente: Cliente

    # Se guardan como dicts planos, no como modelos Pydantic: el checkpointer
    # serializa sin tipos custom y el estado sobrevive a cambios de esquema.
    solicitudes_detectadas: list[dict[str, Any]]

    # reducer aditivo: cada rama del fan-out anexa su resultado
    solicitudes: Annotated[list[dict[str, Any]], operator.add]

    caso: dict[str, Any]           # lo que se le muestra al ejecutivo Care
    decision_humana: dict[str, Any]
    resultado: dict[str, Any]      # ejecucion post-aprobacion (+ `cierre` al final)
    verificacion: dict[str, Any]   # Fase 2: lo que se comprobo y por que no cierra solo
    decision_cierre: dict[str, Any]  # Fase 2: lo que decidio el ejecutivo en la revision de cierre
    traza: Annotated[list[str], operator.add]


class SolicitudState(TypedDict):
    """Estado de entrada de cada rama paralela."""

    solicitud: dict[str, Any]
    cliente: Cliente
    correo: CorreoEntrante
