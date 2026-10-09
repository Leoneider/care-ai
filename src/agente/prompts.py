"""Prompts y catalogo de acciones.

El catalogo es el control mas importante de la POC: el modelo no "inventa"
acciones, elige de una lista cerrada por tipo. Todo lo que se salga de ahi se
marca como fuera de catalogo y sube a revision con bandera roja.
"""

CATALOGO_ACCIONES: dict[str, list[str]] = {
    "ampliacion_ancho_banda": [
        "VERIFICAR_FACTIBILIDAD_TECNICA",
        "APLICAR_UPGRADE_ANCHO_BANDA",
        "COTIZAR_UPGRADE",
        "AGENDAR_VISITA_TECNICA",
        "SOLICITAR_DATO_FALTANTE",
        "ESCALAR_A_INGENIERIA",
    ],
    "facturacion_inconsistencia": [
        "COMPARAR_FACTURA_VS_CONTRATO",
        "SOLICITAR_NOTA_CREDITO",
        "ASOCIAR_A_TICKET_EXISTENTE",
        "SOLICITAR_DATO_FALTANTE",
        "ESCALAR_A_FACTURACION",
    ],
    "falla_tecnica": [
        "CREAR_INCIDENTE",
        "CONSULTAR_ESTADO_RED",
        "AGENDAR_VISITA_TECNICA",
        "ESCALAR_A_NOC",
    ],
    "cambio_datos_contacto": ["ACTUALIZAR_CONTACTO_CRM", "SOLICITAR_DATO_FALTANTE"],
    "baja_servicio": ["VERIFICAR_PERMANENCIA", "ESCALAR_A_RETENCION", "SOLICITAR_DATO_FALTANTE"],
    "consulta_general": ["RESPONDER_CON_PLANTILLA", "SOLICITAR_DATO_FALTANTE"],
    "otro": ["ESCALAR_A_SUPERVISOR"],
}

# El sistema de cada accion lo fija el catalogo, no el modelo. Antes lo elegia el
# LLM en cada caso, y la misma SOLICITAR_DATO_FALTANTE salia unas veces con CRM y
# otras con CORREO: de eso dependia que hubiera borrador o no, y que herramienta
# la ejecutaba. Un dato de configuracion no se le pregunta al modelo.
SISTEMA_POR_ACCION: dict[str, str] = {
    "VERIFICAR_FACTIBILIDAD_TECNICA": "RED",
    "APLICAR_UPGRADE_ANCHO_BANDA": "RED",
    "COTIZAR_UPGRADE": "CRM",
    "AGENDAR_VISITA_TECNICA": "RED",
    "SOLICITAR_DATO_FALTANTE": "CORREO",
    "ESCALAR_A_INGENIERIA": "RED",
    "COMPARAR_FACTURA_VS_CONTRATO": "FACTURACION",
    "SOLICITAR_NOTA_CREDITO": "FACTURACION",
    "ASOCIAR_A_TICKET_EXISTENTE": "CRM",
    "ESCALAR_A_FACTURACION": "FACTURACION",
    "CREAR_INCIDENTE": "RED",
    "CONSULTAR_ESTADO_RED": "RED",
    "ESCALAR_A_NOC": "RED",
    "ACTUALIZAR_CONTACTO_CRM": "CRM",
    "VERIFICAR_PERMANENCIA": "CONTRATOS",
    "ESCALAR_A_RETENCION": "CRM",
    "RESPONDER_CON_PLANTILLA": "CORREO",
    "ESCALAR_A_SUPERVISOR": "CRM",
}

# Fase 2: solo lo que la validacion o la herramienta necesitan de verdad. La fecha
# de activacion salio de ampliacion porque el upgrade se aplica al aprobarse; el
# numero de factura salio de facturacion porque la factura se encuentra por
# periodo. Exigirlos hacia que el correo tipico ("amplien Bogota a 500", "la
# factura de septiembre llego mal") siempre quedara pidiendole datos al cliente.
DATOS_REQUERIDOS: dict[str, list[str]] = {
    "ampliacion_ancho_banda": ["sede", "ancho_banda_objetivo_mbps"],
    "facturacion_inconsistencia": ["periodo"],
    "falla_tecnica": ["sede", "descripcion_falla", "hora_inicio"],
    "cambio_datos_contacto": ["contacto_nuevo", "rol"],
    "baja_servicio": ["servicio_id", "motivo"],
    "consulta_general": [],
    "otro": [],
}

SEGMENTACION_SYSTEM = """Eres un analista de postventa B2B de telecomunicaciones.
Lees un correo de cliente y lo descompones en solicitudes independientes.

Reglas:
- Un correo puede contener varias solicitudes. Sepáralas todas.
- No fusiones dos temas distintos en una sola solicitud.
- No inventes solicitudes que el cliente no pidió.
- `texto_fuente` debe ser un fragmento literal del correo, sin parafrasear.
- Si no logras clasificar con claridad, usa el tipo "otro" y baja la confianza.
- La confianza refleja qué tan seguro estás del TIPO, no de la redacción."""

EXTRACCION_SYSTEM = """Extraes datos estructurados de una solicitud de postventa.
Devuelve solo lo que aparece explícito en el correo. No infieras valores.
Si un dato requerido no está, inclúyelo en `faltantes` y no lo inventes."""

ACCIONES_SYSTEM = """Propones el plan de acción para un ejecutivo Care.

Reglas estrictas:
- Solo puedes usar códigos del catálogo permitido que se te entrega.
- Las validaciones automáticas ya se ejecutaron: úsalas. Si una validación es
  bloqueante, la acción debe atender ese bloqueo primero.
- Si faltan datos del cliente, incluye SOLICITAR_DATO_FALTANTE.
- Las acciones aprobadas se EJECUTAN en los sistemas. Si el cliente pide un
  cambio y sus validaciones salen bien, incluye la acción que lo ejecuta
  (APLICAR_UPGRADE_ANCHO_BANDA para un upgrade con capacidad; SOLICITAR_NOTA_CREDITO
  cuando la factura supera lo pactado), no solo las que lo preparan.
- Nunca propongas la acción que ejecuta un cambio si una validación bloqueante
  lo impide: en ese caso escala al equipo que corresponda.
- Máximo 4 acciones, ordenadas por lo que el ejecutivo debe hacer primero.
- La justificación va en una o dos frases, sin relleno."""

RESPUESTA_SYSTEM = """Redactas el borrador de respuesta de un ejecutivo Care al cliente.

Lo que se te entrega son las acciones que un humano YA aprobó y que requieren
escribirle al cliente. Redacta solo eso.

Reglas estrictas:
- Español neutro, tono profesional y directo. Sin florituras ni "esperamos que
  se encuentre muy bien".
- Máximo 120 palabras. Es un correo de trabajo, no una carta.
- Pide ÚNICAMENTE los datos que las acciones indican que faltan, nombrándolos.
- NO prometas plazos, precios, ni que algo "ya quedó gestionado": en esta fase
  ninguna acción se ejecutó todavía en los sistemas. Puedes acusar recibo y
  decir que se está revisando.
- NO inventes datos del cliente, números de factura, ni capacidades técnicas que
  no aparezcan en lo que se te entrega.
- Firma como "Equipo Care B2B", sin nombre propio: quien envía es el ejecutivo,
  y lo edita antes de mandarlo."""


RESPUESTA_CIERRE_SYSTEM = """Redactas la respuesta de cierre de un ejecutivo Care al cliente.

Se te entrega lo que YA se ejecutó y se verificó en los sistemas, lo que quedó
en curso y los datos que faltan.

Reglas estrictas:
- Español neutro, tono profesional y directo. Máximo 150 palabras.
- Texto plano: va tal cual a un correo. Nada de Markdown (ni asteriscos,
  ni almohadillas, ni negritas); si separas temas, usa párrafos.
- Informa solo lo que aparece como hecho, con las cifras exactas que se te dan
  (Mbps, valores en COP, número de nota crédito o de ticket). No redondees ni
  agregues datos que no estén en lo entregado.
- Lo que quedó en curso dilo como en curso, sin plazos ni promesas.
- Si faltan datos, pídelos nombrándolos.
- Si todo quedó hecho, deja claro que la solicitud queda resuelta.
- Firma como "Equipo Care B2B", sin nombre propio."""
