"""Validaciones deterministas.

Esta es la parte que hace util al copiloto y que NO debe hacer el LLM.
El modelo entiende el correo; el codigo verifica contra los sistemas.
Cada validacion devuelve ok / alerta / falla, y puede ser bloqueante.

Los helpers de arriba (`buscar_servicio`, `numero`, `normalizar_periodo`,
`buscar_factura`) los usa tambien `ejecucion.py`: la herramienta que aplica el
cambio tiene que encontrar el mismo servicio y la misma factura que se validaron.
"""

import re
import unicodedata
from datetime import date
from typing import Any, Callable

from . import backends
from .schemas import Validacion


# --------------------------------------------------------------------------- #
# Lectura robusta de lo que extrae el modelo
# --------------------------------------------------------------------------- #
# El modelo extrae lo que el cliente escribio, y el cliente escribe "Bogotá",
# "500 Mbps" o "la de septiembre". Comparar eso literal contra los sistemas es la
# forma mas facil de que una validacion falle sin que el caso tenga nada malo.

def sin_tildes(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", texto or "") if not unicodedata.combining(c))


def _palabras(texto: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", sin_tildes(texto).lower())


_RELLENO = {"sede", "de", "del", "la", "el", "los", "las", "en", "campus", "oficina", "principal"}


def buscar_servicio(cliente: dict[str, Any], sede: str) -> dict[str, Any] | None:
    """El servicio de la sede que menciona el cliente.

    Primero la sede completa como secuencia de palabras ("Bogota" dentro de
    "Bogota - Sede Chapinero"); si no, cualquier palabra significativa ("sede de
    Bogotá" -> "bogota"). Sin tildes ni mayusculas en ningun lado.
    """
    buscada = " ".join(_palabras(sede))
    if not buscada:
        return None
    servicios = cliente.get("servicios", [])
    # Palabras completas: "cali" no debe encontrar "Bogota - Calle 100".
    for s in servicios:
        if f" {buscada} " in f" {' '.join(_palabras(s['sede']))} ":
            return s
    significativas = [w for w in buscada.split() if len(w) > 2 and w not in _RELLENO]
    for s in servicios:
        if significativas and set(significativas) & set(_palabras(s["sede"])):
            return s
    return None


def numero(texto: Any) -> int:
    """'500', '500 Mbps', '1.000 Mbps' o '1 Gbps' -> Mbps enteros. 0 si no hay numero."""
    t = str(texto or "").lower()
    t = re.sub(r"(?<=\d)[.,](?=\d{3}\b)", "", t)          # separador de miles
    m = re.search(r"\d+(?:[.,]\d+)?", t)
    if not m:
        return 0
    valor = float(m.group().replace(",", "."))
    if re.search(r"\d\s*g(b|bps|ig)", t):
        valor *= 1000
    return int(valor)


_MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
    "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}
_ABREVIADOS = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7,
               "ago": 8, "sep": 9, "sept": 9, "set": 9, "oct": 10, "nov": 11, "dic": 12}


def normalizar_periodo(texto: str, hoy: date | None = None) -> str:
    """'septiembre', 'sept 2026', '09/2026' o '2026-09' -> '2026-09'.

    Sin ano explicito se asume el ultimo mes con ese nombre que ya paso: en
    octubre, "la de diciembre" es la del ano anterior. Si no se reconoce nada,
    devuelve el texto tal cual y la validacion dira que no encontro la factura.
    """
    hoy = hoy or date.today()
    t = sin_tildes(texto or "").lower().strip()
    if m := re.search(r"\b(20\d{2})[-/](\d{1,2})\b", t):
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    if m := re.search(r"\b(\d{1,2})[-/](20\d{2})\b", t):
        return f"{m.group(2)}-{int(m.group(1)):02d}"

    palabras = _palabras(t)
    mes = next((_MESES[w] for w in palabras if w in _MESES), None)
    mes = mes or next((_ABREVIADOS[w] for w in palabras if w in _ABREVIADOS), None)
    if not mes:
        return texto
    anio = next((int(w) for w in palabras if re.fullmatch(r"20\d{2}", w)), None)
    if anio is None:
        anio = hoy.year if mes <= hoy.month else hoy.year - 1
    return f"{anio}-{mes:02d}"


def buscar_factura(cuenta_id: str, datos: dict[str, str]) -> dict[str, Any] | None:
    """Por numero si el cliente lo dio; si no, por periodo."""
    facturas = backends.facturacion_obtener_facturas(cuenta_id)
    numero_factura = (datos.get("factura") or "").strip().upper()
    if numero_factura:
        exacta = next((f for f in facturas if f["factura"].upper() == numero_factura), None)
        if exacta:
            return exacta
    periodo = normalizar_periodo(datos.get("periodo", ""))
    return next((f for f in facturas if f["periodo"] == periodo), None)


# --------------------------------------------------------------------------- #
# Validadores por tipo
# --------------------------------------------------------------------------- #
def _validar_ampliacion(cliente: dict, datos: dict[str, str]) -> list[Validacion]:
    out: list[Validacion] = []
    sede = datos.get("sede", "")
    servicio = buscar_servicio(cliente, sede)

    if not servicio:
        out.append(
            Validacion(
                check="servicio_identificado",
                resultado="falla",
                detalle=f"No se identifico un servicio activo para la sede '{sede or 'no indicada'}'.",
                bloqueante=True,
            )
        )
        return out

    out.append(
        Validacion(
            check="servicio_identificado",
            resultado="ok",
            detalle=f"{servicio['servicio_id']} - {servicio['producto']} "
            f"{servicio['ancho_banda_mbps']} Mbps en {servicio['sede']}.",
        )
    )

    objetivo = numero(datos.get("ancho_banda_objetivo_mbps"))
    delta = objetivo - servicio["ancho_banda_mbps"]
    capacidad = backends.red_consultar_capacidad(servicio["nodo"])

    if delta <= 0:
        out.append(
            Validacion(
                check="delta_ancho_banda",
                resultado="alerta",
                detalle="El ancho de banda solicitado no supera al contratado. Confirmar con el cliente.",
                bloqueante=True,
            )
        )
    elif capacidad["capacidad_libre_mbps"] >= delta and capacidad["estado"] == "saludable":
        out.append(
            Validacion(
                check="capacidad_nodo",
                resultado="ok",
                detalle=f"Nodo {servicio['nodo']} con {capacidad['capacidad_libre_mbps']} Mbps libres "
                f"para un incremento de {delta} Mbps.",
            )
        )
    else:
        out.append(
            Validacion(
                check="capacidad_nodo",
                resultado="falla",
                detalle=f"Nodo {servicio['nodo']} en estado '{capacidad['estado']}' con "
                f"{capacidad['capacidad_libre_mbps']} Mbps libres. Requiere ingenieria.",
                bloqueante=True,
            )
        )

    contrato = backends.contratos_obtener(cliente.get("cuenta_id", ""))
    if contrato:
        out.append(
            Validacion(
                check="contrato_permite_upgrade",
                resultado="ok" if contrato["permite_upgrade_sin_adendum"] else "alerta",
                detalle=f"Contrato {contrato['contrato']} vigente hasta {contrato['vigencia_hasta']}. "
                + ("Permite upgrade sin adendum." if contrato["permite_upgrade_sin_adendum"] else "Requiere adendum."),
            )
        )
    return out


def _validar_facturacion(cliente: dict, datos: dict[str, str]) -> list[Validacion]:
    out: list[Validacion] = []
    cuenta = cliente.get("cuenta_id", "")
    factura = buscar_factura(cuenta, datos)

    if not factura:
        periodo = datos.get("periodo", "")
        out.append(
            Validacion(
                check="factura_identificada",
                resultado="falla",
                detalle=f"No se encontro factura para el periodo '{periodo or 'no indicado'}'.",
                bloqueante=True,
            )
        )
        return out

    neto = factura.get("valor_neto", factura["valor"])
    ajuste = (
        f" ({factura['ajustes']:,} COP en notas credito, neto {neto:,} COP)" if factura.get("ajustes") else ""
    )
    out.append(
        Validacion(
            check="factura_identificada",
            resultado="ok",
            detalle=f"{factura['factura']} periodo {factura['periodo']}, "
            f"valor {factura['valor']:,} COP{ajuste}, estado {factura['estado']}.",
        )
    )

    contrato = backends.contratos_obtener(cuenta)
    if contrato:
        pactado = contrato["valor_mensual_pactado"]
        diff = neto - pactado
        if diff != 0:
            out.append(
                Validacion(
                    check="factura_vs_contrato",
                    resultado="alerta",
                    detalle=f"Diferencia de {diff:,} COP frente al valor pactado ({pactado:,} COP). "
                    "El reclamo del cliente tiene sustento.",
                )
            )
        else:
            out.append(
                Validacion(
                    check="factura_vs_contrato",
                    resultado="ok",
                    detalle="El valor facturado coincide con el contrato.",
                )
            )

    abiertos = backends.tickets_abiertos(cuenta, tema="facturacion")
    if abiertos:
        out.append(
            Validacion(
                check="ticket_duplicado",
                resultado="alerta",
                detalle=f"Ya existe {abiertos[0]['ticket']} abierto por facturacion. Evitar duplicar caso.",
            )
        )
    return out


VALIDADORES: dict[str, Callable[[dict, dict[str, str]], list[Validacion]]] = {
    "ampliacion_ancho_banda": _validar_ampliacion,
    "facturacion_inconsistencia": _validar_facturacion,
}


def validar(tipo: str, cliente: dict[str, Any], datos: dict[str, str]) -> list[Validacion]:
    validador = VALIDADORES.get(tipo)
    if not validador:
        return [
            Validacion(
                check="sin_validador",
                resultado="alerta",
                detalle=f"No hay validaciones automaticas para el tipo '{tipo}'. Revision manual completa.",
            )
        ]
    return validador(cliente, datos)
