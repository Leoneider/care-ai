"""Respuestas simuladas del LLM para el correo de ejemplo.

No intentan ser inteligentes: reproducen el caso del enunciado (ampliacion de
ancho de banda + inconsistencia de facturacion en el mismo correo) para ejercitar
el fan-out, el interrupt y, en la Fase 2, la ejecucion completa: el plan incluye
las acciones que aplican el upgrade y emiten la nota credito.

`CORREO_EJEMPLO` es ese correo. Solo lo usan las pruebas: nada lo siembra en la
bandeja de trabajo, los casos entran unicamente por el poller de Gmail.
"""

from typing import Any

from langsmith import traceable

from .schemas import (
    AccionPropuesta,
    DatosExtraidos,
    PlanAcciones,
    RespuestaCliente,
    Segmentacion,
    SolicitudDetectada,
)

CORREO_EJEMPLO = {
    "message_id": "<CAF-2026-0921-77120@ufpso.edu.co>",
    "thread_id": "THR-77120",
    "remitente": "ltrigosg@ufpso.edu.co",
    "para": ["care.b2b@operador.com"],
    "asunto": "Ampliacion sede Bogota + revision factura septiembre",
    "cuerpo": (
        "Buenos dias,\n\n"
        "Necesitamos ampliar el ancho de banda de la sede de Bogota a 500 Mbps, "
        "el trafico nos viene creciendo y ya estamos al tope.\n\n"
        "Aprovecho tambien: la factura de septiembre llego por un valor distinto "
        "al pactado en el contrato. Les agradezco la revision.\n\n"
        "Gracias,\nLeoneider Trigos\n"
        "--\nLeoneider Trigos | UFPSO\n"
    ),
    "adjuntos": [],
}

# Una consulta sin nada que cambiar: el cliente pregunta por lo que tiene. Ejercita
# el validador de `consulta_general` y la respuesta de cierre con el plan.
CORREO_CONSULTA = {
    "message_id": "<CAF-2026-1010-plan@ufpso.edu.co>",
    "thread_id": "THR-PLAN",
    "remitente": "ltrigosg@ufpso.edu.co",
    "para": ["care.b2b@operador.com"],
    "asunto": "Consulta sobre mi plan",
    "cuerpo": "Hola, me pueden ayudar con una consulta sobre mi plan actual.\n",
    "adjuntos": [],
}


class FakeStructuredLLM:
    def __init__(self, schema: type):
        self.schema = schema

    def invoke(self, mensajes: Any) -> Any:
        # El fake no es un Runnable de LangChain, asi que LangSmith no lo veria:
        # la traza del modo offline mostraria los nodos pero ningun prompt.
        # `traceable` lo reporta como un span LLM y deja las dos trazas (fake y
        # modelo real) con la misma forma. Con el tracing apagado es un no-op.
        return self._responder(
            mensajes, langsmith_extra={"name": f"FakeLLM::{self.schema.__name__}"}
        )

    @traceable(run_type="llm")
    def _responder(self, mensajes: Any) -> Any:
        texto = str(mensajes).lower()

        if self.schema is Segmentacion:
            if "plan actual" in texto:
                return Segmentacion(
                    solicitudes=[
                        SolicitudDetectada(
                            id="s1",
                            tipo="consulta_general",
                            resumen="El cliente solicita informacion sobre su plan actual.",
                            texto_fuente="una consulta sobre mi plan actual",
                            confianza=0.85,
                        )
                    ]
                )
            return Segmentacion(
                solicitudes=[
                    SolicitudDetectada(
                        id="s1",
                        tipo="ampliacion_ancho_banda",
                        resumen="El cliente solicita ampliar el ancho de banda de la sede Bogota a 500 Mbps.",
                        texto_fuente="necesitamos ampliar el ancho de banda de la sede de Bogota a 500 Mbps",
                        confianza=0.93,
                    ),
                    SolicitudDetectada(
                        id="s2",
                        tipo="facturacion_inconsistencia",
                        resumen="El cliente reporta una diferencia en la factura de septiembre frente a lo pactado.",
                        texto_fuente="la factura de septiembre llego por un valor distinto al pactado",
                        confianza=0.88,
                    ),
                ]
            )

        if self.schema is DatosExtraidos:
            # Lo que devolveria el modelo real leyendo el correo tal cual: la sede
            # como la escribe el cliente y el periodo en palabras. Asi el modo
            # offline tambien ejercita la normalizacion de `validaciones.py`.
            if "tipo de solicitud: facturacion" in texto:
                return DatosExtraidos(campos={"periodo": "septiembre"}, faltantes=[])
            if "tipo de solicitud: consulta_general" in texto:
                return DatosExtraidos(campos={}, faltantes=[])
            # Cucuta corre sobre CUC-N07, que en el mini sistema esta saturado:
            # es la unica forma de ejercitar una validacion bloqueante en modo
            # offline, y por tanto la friccion que la UI impone al aprobarla.
            if "cucuta" in texto:
                return DatosExtraidos(
                    campos={"sede": "Cucuta", "ancho_banda_objetivo_mbps": "500"},
                    faltantes=["fecha_deseada_activacion"],
                )
            return DatosExtraidos(
                campos={"sede": "Bogota", "ancho_banda_objetivo_mbps": "500 Mbps"},
                faltantes=[],
            )

        if self.schema is PlanAcciones:
            if "tipo: consulta_general" in texto:
                return PlanAcciones(
                    acciones=[
                        AccionPropuesta(
                            codigo="RESPONDER_CON_PLANTILLA",
                            descripcion="Responder al cliente con el detalle de su plan actual.",
                            sistema="CORREO",
                            parametros={},
                        )
                    ],
                    justificacion="Es una consulta: no hay nada que cambiar, solo informar el plan.",
                )
            if "tipo: facturacion_inconsistencia" in texto:
                acciones = [
                    AccionPropuesta(
                        codigo="COMPARAR_FACTURA_VS_CONTRATO",
                        descripcion="Contrastar la factura de septiembre contra el valor mensual pactado.",
                        sistema="FACTURACION",
                        parametros={"periodo": "2026-09"},
                    ),
                    AccionPropuesta(
                        codigo="SOLICITAR_NOTA_CREDITO",
                        descripcion="Emitir nota credito por la diferencia frente a lo pactado.",
                        sistema="FACTURACION",
                        parametros={"periodo": "2026-09"},
                    ),
                ]
                if "ticket_duplicado" in texto:
                    acciones.append(
                        AccionPropuesta(
                            codigo="ASOCIAR_A_TICKET_EXISTENTE",
                            descripcion="Vincular el reporte al ticket de facturacion ya abierto.",
                            sistema="CRM",
                            parametros={"tema": "facturacion"},
                        )
                    )
                return PlanAcciones(
                    acciones=acciones,
                    justificacion="La factura supera lo pactado en el contrato.",
                )
            if "saturado" in texto:
                return PlanAcciones(
                    acciones=[
                        AccionPropuesta(
                            codigo="ESCALAR_A_INGENIERIA",
                            descripcion="Escalar a ingenieria: el nodo no tiene capacidad para el aumento.",
                            sistema="RED",
                            parametros={"sede": "Cucuta", "objetivo_mbps": "500"},
                        ),
                        AccionPropuesta(
                            codigo="SOLICITAR_DATO_FALTANTE",
                            descripcion="Pedir al cliente la fecha deseada de activacion.",
                            sistema="CORREO",
                            parametros={"dato": "fecha_deseada_activacion"},
                        ),
                    ],
                    justificacion="El nodo esta saturado: el upgrade no se puede aplicar sin ingenieria.",
                )
            return PlanAcciones(
                acciones=[
                    AccionPropuesta(
                        codigo="VERIFICAR_FACTIBILIDAD_TECNICA",
                        descripcion="Confirmar capacidad del nodo antes de aplicar el cambio.",
                        sistema="RED",
                        parametros={"sede": "Bogota"},
                    ),
                    AccionPropuesta(
                        codigo="APLICAR_UPGRADE_ANCHO_BANDA",
                        descripcion="Aplicar el upgrade a 500 Mbps en la sede Bogota.",
                        sistema="RED",
                        parametros={"sede": "Bogota", "objetivo_mbps": "500"},
                    ),
                ],
                justificacion="El nodo tiene capacidad y el contrato permite upgrade sin adendum.",
            )

        if self.schema is RespuestaCliente:
            if "respuesta de cierre" in texto and "plan actual" in texto:
                return RespuestaCliente(
                    asunto="Consulta sobre mi plan",
                    cuerpo=(
                        "Hola,\n\n"
                        "Su plan actual consta de tres servicios de Internet Dedicado: 200 Mbps en "
                        "Bogota - Sede Chapinero, 100 Mbps en Cucuta - Sede Centro y 300 Mbps en "
                        "Ocana - Campus Algodonal, bajo el contrato CTO-3107 vigente hasta 2027-06-30 "
                        "por 6,200,000 COP mensuales.\n\n"
                        "Con esto su consulta queda resuelta.\n\n"
                        "Equipo Care B2B"
                    ),
                )
            if "respuesta de cierre" in texto:
                return RespuestaCliente(
                    asunto="Ampliacion sede Bogota + revision factura septiembre",
                    cuerpo=(
                        "Hola,\n\n"
                        "Le confirmamos que la sede de Bogota quedo ampliada a 500 Mbps y que "
                        "emitimos una nota credito por la diferencia de la factura de septiembre "
                        "frente a lo pactado en el contrato.\n\n"
                        "Con esto su solicitud queda resuelta.\n\n"
                        "Equipo Care B2B"
                    ),
                )
            return RespuestaCliente(
                asunto="Ampliacion sede Bogota + revision factura septiembre",
                cuerpo=(
                    "Hola,\n\n"
                    "Recibimos su solicitud de ampliacion de ancho de banda y el reporte "
                    "sobre la factura de septiembre; ambos estan en revision.\n\n"
                    "Para avanzar con la ampliacion necesitamos un dato: la fecha deseada "
                    "de activacion.\n\n"
                    "Quedamos atentos.\n\n"
                    "Equipo Care B2B"
                ),
            )

        raise NotImplementedError(f"Sin fake para {self.schema}")
