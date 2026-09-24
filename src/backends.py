"""La costura con los sistemas internos.

Hasta la Fase 1 esto eran diccionarios. Ahora cada funcion habla con el mini
sistema legado de `sistemas.py`, que tiene su propia base y se puede modificar.
En el piloto, cada funcion se reemplaza por una llamada real (API / vista de
datos) manteniendo la MISMA firma. Ese es el punto: el grafo no se entera.

Dos familias:

- **Lectura** (`crm_*`, `facturacion_obtener_*`, `contratos_*`, `red_consultar_*`,
  `tickets_abiertos`): las llaman las validaciones deterministas. No las llama el
  LLM libremente, las llama el nodo segun el tipo de solicitud. Asi controlamos
  costo, latencia y sobre todo el blast radius.
- **Escritura** (Fase 2): las llama `ejecucion.py` para las acciones que un humano
  ya aprobo. Cada una deja rastro en la bitacora del sistema.
"""

import os
from email.utils import parseaddr
from typing import Any

from . import db, sistemas

# Para que `ejecucion.py` distinga un rechazo del sistema de un bug.
ErrorSistema = sistemas.ErrorSistema


def _correos_demo() -> set[str]:
    """Direcciones que se hacen pasar por contacto de ACME, via CARE_CORREO_DEMO.

    ANDAMIO DE DEMO, no parte del diseno. Existe para poder probar contra un buzon
    de Gmail real: si no, todo correo enviado desde una cuenta personal cae en
    "NO IDENTIFICADA" y el caso no ejercita ninguna validacion contra facturacion,
    red ni contratos — que es justo la mitad interesante.

    Se borra el dia que `crm_buscar_cuenta_por_correo` consulte el CRM de verdad.
    """
    return {c.strip().lower() for c in os.getenv("CARE_CORREO_DEMO", "").split(",") if c.strip()}


# --------------------------------------------------------------------------- #
# Lectura
# --------------------------------------------------------------------------- #
def crm_buscar_cuenta_por_correo(correo: str) -> dict[str, Any]:
    # parseaddr porque un buzon real no manda la direccion pelada: el header From
    # llega como 'Laura Gomez <laura.gomez@acme.com.co>' y un split('@') crudo
    # devolveria el dominio con el '>' pegado, sin encontrar nunca al cliente.
    correo = (parseaddr(correo)[1] or correo).lower().strip()
    dominio = correo.split("@")[-1]

    # Primero el contacto exacto, como haria un CRM real; el dominio solo identifica
    # la cuenta, no autoriza a quien escribe.
    cuenta, autorizado = sistemas.cuenta_por_contacto(correo)
    if cuenta is None:
        cuenta, autorizado = sistemas.cuenta_por_dominio(dominio), False

    if cuenta is None and correo in _correos_demo():
        cuenta, autorizado = sistemas.cuenta_por_dominio("acme.com.co"), True

    if not cuenta:
        return {"encontrado": False}
    return {
        "encontrado": True,
        "cuenta_id": cuenta["cuenta_id"],
        "razon_social": cuenta["razon_social"],
        "segmento": cuenta["segmento"],
        "estado": cuenta["estado"],
        "contacto_autorizado": autorizado,
        "servicios": sistemas.servicios(cuenta["cuenta_id"]),
    }


def facturacion_obtener_facturas(cuenta_id: str) -> list[dict[str, Any]]:
    """Cada factura trae `valor` (lo facturado), `ajustes` (notas credito) y
    `valor_neto`. Las validaciones comparan el neto: una factura ya corregida con
    nota credito no debe volver a salir como reclamo con sustento."""
    return sistemas.facturas(cuenta_id)


def contratos_obtener(cuenta_id: str) -> dict[str, Any]:
    return sistemas.contrato(cuenta_id)


def red_consultar_capacidad(nodo: str) -> dict[str, Any]:
    return sistemas.nodo(nodo) or {"capacidad_libre_mbps": 0, "estado": "desconocido"}


def tickets_abiertos(cuenta_id: str, tema: str | None = None) -> list[dict[str, Any]]:
    return sistemas.tickets(cuenta_id, tema=tema, solo_abiertos=True)


def red_obtener_servicio(servicio_id: str) -> dict[str, Any] | None:
    return sistemas.servicio(servicio_id)


def facturacion_obtener_factura(numero: str) -> dict[str, Any] | None:
    return sistemas.factura(numero)


def tickets_obtener(numero: str) -> dict[str, Any] | None:
    return sistemas.ticket(numero)


# --------------------------------------------------------------------------- #
# Escritura (Fase 2)
# --------------------------------------------------------------------------- #
def red_aplicar_ancho_banda(servicio_id: str, mbps: int, caso_id: str = "") -> dict[str, Any]:
    return sistemas.aplicar_ancho_banda(servicio_id, mbps, caso_id)


def facturacion_emitir_nota_credito(factura: str, valor: int, motivo: str, caso_id: str = "") -> dict[str, Any]:
    return sistemas.emitir_nota_credito(factura, valor, motivo, caso_id)


def tickets_crear(cuenta_id: str, tema: str, asunto: str, *, grupo: str = "Care",
                  estado: str = "en_proceso", caso_id: str = "") -> str:
    return sistemas.crear_ticket(cuenta_id, tema, asunto, grupo=grupo, estado=estado, caso_id=caso_id)


def tickets_actualizar(ticket: str, *, estado: str | None = None, nota: str = "", caso_id: str = "") -> None:
    sistemas.actualizar_ticket(ticket, estado=estado, nota=nota, caso_id=caso_id)


# --- idempotencia: correos ya procesados -------------------------------------
# Era un set() en memoria. Con un buzon real eso significa reprocesar el correo
# entero en cada reinicio del poller, asi que vive en SQLite (ver db.py). Las
# firmas no cambian: el nodo `ingesta` no se entera.


def ya_procesado(message_id: str) -> bool:
    return db.ya_procesado(message_id)


def marcar_procesado(message_id: str) -> None:
    """Idempotente. Normalmente la fila ya existe porque `servicio.procesar_correo`
    la reservo antes de arrancar el grafo; esto cubre a quien invoque el grafo a
    mano (el demo, un test) sin pasar por la capa de servicio."""
    db.marcar_procesado_directo(message_id)
