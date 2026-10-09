"""Contratos de datos: todo lo que el LLM produce sale tipado con Pydantic.

Regla de oro de la POC: el LLM nunca devuelve texto libre que luego parseamos.
Siempre structured output -> validable, testeable, comparable contra un golden set.
"""

from typing import Literal

from pydantic import BaseModel, Field

# Taxonomia de postventa B2B. Empezar corto: 6-8 tipos maximo.
# Si el modelo duda, existe "otro" y eso dispara escalamiento humano directo.
TipoSolicitud = Literal[
    "ampliacion_ancho_banda",
    "facturacion_inconsistencia",
    "falla_tecnica",
    "cambio_datos_contacto",
    "baja_servicio",
    "consulta_general",
    "otro",
]


class SolicitudDetectada(BaseModel):
    """Una intencion dentro del correo. Un correo puede traer varias."""

    id: str = Field(description="Identificador corto y unico dentro del correo, ej. 's1'")
    tipo: TipoSolicitud
    resumen: str = Field(description="Una linea, en tercera persona, sin adjetivos")
    texto_fuente: str = Field(
        description="Fragmento literal del correo que sustenta esta solicitud. "
        "Sirve para auditar y para que el ejecutivo verifique rapido."
    )
    confianza: float = Field(ge=0, le=1)


class Segmentacion(BaseModel):
    solicitudes: list[SolicitudDetectada]


class DatosExtraidos(BaseModel):
    """Entidades relevantes segun el tipo. Clave-valor para no casarnos con un
    esquema rigido en la POC; en produccion se vuelve un modelo por tipo."""

    campos: dict[str, str] = Field(default_factory=dict)
    faltantes: list[str] = Field(
        default_factory=list,
        description="Datos que el tipo de solicitud exige y el correo no trae",
    )


class AccionPropuesta(BaseModel):
    codigo: str = Field(description="Debe pertenecer al catalogo permitido para el tipo")
    descripcion: str
    sistema: str = Field(description="CRM | FACTURACION | RED | CONTRATOS | CORREO")
    parametros: dict[str, str] = Field(default_factory=dict)


class PlanAcciones(BaseModel):
    acciones: list[AccionPropuesta]
    justificacion: str


class Validacion(BaseModel):
    """Resultado de un chequeo determinista contra sistemas. No lo genera el LLM."""

    check: str
    resultado: Literal["ok", "alerta", "falla"]
    detalle: str
    bloqueante: bool = False


class SolicitudProcesada(BaseModel):
    """Lo que el ejecutivo Care ve como una tarjeta en su bandeja."""

    id: str
    tipo: TipoSolicitud
    resumen: str
    texto_fuente: str
    datos: dict[str, str] = Field(default_factory=dict)
    faltantes: list[str] = Field(default_factory=list)
    validaciones: list[Validacion] = Field(default_factory=list)
    acciones: list[AccionPropuesta] = Field(default_factory=list)
    confianza: float = 0.0
    requiere_atencion: bool = False
    motivo_atencion: str = ""


class RespuestaCliente(BaseModel):
    """Borrador de respuesta al cliente. Nunca se envia solo: se deja como
    borrador en el hilo de Gmail para que el ejecutivo lo revise y lo mande."""

    asunto: str = Field(description="Asunto de la respuesta, sin el prefijo 'Re:'")
    cuerpo: str = Field(
        description="Texto plano, en espanol, tono de postventa B2B. "
        "Saludo, el asunto concreto, la peticion o el acuse, y despedida."
    )
