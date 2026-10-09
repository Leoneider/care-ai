"""Consola del mini sistema legado: CRM, facturacion, contratos, red y mesa de servicio.

Es una aplicacion aparte de la bandeja de Care AI, con su propio proceso y puerto:

    python -m src.sistemas_web        # http://127.0.0.1:8002

En produccion estos cinco sistemas son de otros equipos y tienen sus propias
pantallas. La POC los separa igual para que la demo cuente la historia correcta:
el agente es una cosa y el sistema es otra. Care AI escribe aqui solo a traves de
`backends.py`, y esta consola solo lee `sistemas.py`: no importa `db` ni
`servicio`, no sabe que hay una bandeja ni un grafo.

Ademas de mirar, la consola da de alta clientes de prueba (cuenta, contactos,
servicios, contrato, facturas) para ensayar con otros correos que los dos de la
semilla. Esas altas sobreviven a "Restaurar": vuelven a su estado inicial.

"Restaurar" devuelve los sistemas a la semilla y, por facilidad para probar la
POC, puede pedirle a la bandeja de Care AI que archive sus casos. Se lo pide por
HTTP (`CARE_BANDEJA_URL`), sin importar su codigo.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request as Request_, urlopen

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import sistemas

PLANTILLAS = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# La cuenta de pruebas del buzon real, que es la de la demo.
CUENTA_DEMO = "B2B-005130"

# Donde escucha la bandeja de Care AI, para vaciarla al restaurar (ver `restaurar`).
BANDEJA_URL = os.getenv("CARE_BANDEJA_URL", "http://127.0.0.1:8000").rstrip("/")


def _fecha(iso: str | None) -> str:
    """ISO 8601 -> 'dd/mm HH:MM'."""
    if not iso:
        return "-"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso[:16]
    # Todo se guarda en UTC; se muestra en la hora local de la maquina (Colombia,
    # UTC-5), que es la que lee el ejecutivo. Sin zona, se asume UTC.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%d/%m %H:%M")


def _cop(valor: Any) -> str:
    """7150000 -> '7.150.000', como se escriben los pesos."""
    try:
        return f"{int(valor):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "-"


def _iniciales(nombre: str) -> str:
    """'UFPSO (cuenta de pruebas)' -> 'UC'. Para el avatar."""
    palabras = [p for p in (nombre or "").replace("(", " ").split() if p[:1].isalnum()]
    return "".join(p[0] for p in palabras[:2]).upper() or "?"


PLANTILLAS.env.filters["iniciales"] = _iniciales
PLANTILLAS.env.filters["fecha"] = _fecha
PLANTILLAS.env.filters["cop"] = _cop
PLANTILLAS.env.filters["dominio"] = sistemas.dominio_visible
PLANTILLAS.env.globals["segmentos"] = sistemas.SEGMENTOS
PLANTILLAS.env.globals["estados_factura"] = sistemas.ESTADOS_FACTURA
PLANTILLAS.env.globals["estados_cuenta"] = sistemas.ESTADOS_CUENTA

app = FastAPI(title="Sistema CORE - POC", docs_url=None, redoc_url=None)


def _volver(cuenta: str = "", pestana: str = "", **aviso: str) -> RedirectResponse:
    """Redireccion a la ficha de la cuenta con un aviso (ok=... o error=...)."""
    params = {k: v for k, v in {"cuenta": cuenta, **aviso}.items() if v}
    return RedirectResponse(f"/?{urlencode(params)}" + (f"#{pestana}" if pestana else ""), status_code=303)


def _contexto(request: Request, **extra: Any) -> dict[str, Any]:
    """Lo que necesita el marco de la ventana en cualquier pantalla."""
    cuentas = sistemas.cuentas()
    return {
        "request": request,
        "cuentas": cuentas,
        "nodos": sistemas.nodos(),
        "ruta_db": str(sistemas.ruta_db()),
        "n_creadas": sum(c["creada"] for c in cuentas),
        **extra,
    }


# --------------------------------------------------------------------------- #
# Lectura
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def ver_cuenta(request: Request, cuenta: str | None = None, restaurado: int = 0,
               bandeja: int | None = None, ok: str = "", error: str = ""):
    ids = [c["cuenta_id"] for c in sistemas.cuentas()]
    elegida = cuenta if cuenta in ids else (CUENTA_DEMO if CUENTA_DEMO in ids else (ids[0] if ids else None))
    return PLANTILLAS.TemplateResponse(
        request, "cuenta.html",
        _contexto(
            request,
            elegida=elegida,
            datos=sistemas.vista_cuenta(elegida) if elegida else None,
            restaurado=bool(restaurado),
            bandeja=bandeja,
            ok=ok,
            error=error,
        ),
    )


# --------------------------------------------------------------------------- #
# Alta de clientes de prueba
# --------------------------------------------------------------------------- #
def _filas(form: Any, prefijo: str, campos: tuple[str, ...]) -> list[dict[str, str]]:
    """Columnas paralelas del formulario (contacto_correo, contacto_nombre...) -> filas.

    Descarta las filas que quedaron vacias: el formulario siempre trae una en blanco.
    """
    columnas = {c: form.getlist(f"{prefijo}_{c}") for c in campos}
    n = max((len(v) for v in columnas.values()), default=0)
    filas = [{c: (columnas[c][i] if i < len(columnas[c]) else "").strip() for c in campos} for i in range(n)]
    return [f for f in filas if any(f.values())]


@app.get("/clientes/nuevo", response_class=HTMLResponse)
def nuevo_cliente(request: Request):
    return PLANTILLAS.TemplateResponse(request, "nuevo.html", _contexto(request, previo=None, error=""))


@app.post("/clientes", response_class=HTMLResponse)
async def crear_cliente(request: Request):
    form = await request.form()
    contactos = _filas(form, "contacto", ("correo", "nombre", "autorizado"))
    servicios = _filas(form, "servicio", ("sede", "producto", "mbps", "nodo"))
    facturas = _filas(form, "factura", ("periodo", "valor", "estado"))
    # Las filas traen selects y valores por defecto (nodo, Mbps, periodo): cuenta como
    # escrita solo la que tiene el campo que el usuario tiene que llenar a mano.
    servicios = [s for s in servicios if s["sede"]]
    facturas = [f for f in facturas if f["valor"]]
    contactos = [c for c in contactos if c["correo"] or c["nombre"]]
    contrato = None
    if form.get("con_contrato"):
        contrato = {
            "vigencia_hasta": form.get("contrato_vigencia", ""),
            "valor_mensual_pactado": form.get("contrato_valor", ""),
            "permite_upgrade_sin_adendum": form.get("contrato_upgrade") == "1",
            "clausula_permanencia_meses": form.get("contrato_permanencia", "0"),
        }
    try:
        cuenta_id = sistemas.crear_cuenta(
            form.get("razon_social", ""),
            segmento=form.get("segmento", "Corporativo"),
            dominio=form.get("dominio", ""),
            estado=form.get("estado", "activo"),
            contactos=[{"correo": c["correo"], "nombre": c["nombre"], "autorizado": c["autorizado"] != "0"}
                       for c in contactos],
            servicios=[{"sede": s["sede"], "producto": s["producto"], "ancho_banda_mbps": s["mbps"],
                        "nodo": s["nodo"]} for s in servicios],
            contrato=contrato,
            facturas=facturas,
        )
    except sistemas.ErrorSistema as e:
        # Se vuelve a pintar el formulario con lo escrito: rehacerlo entero por un typo es lo peor.
        previo = {
            "form": {k: form.get(k, "") for k in ("razon_social", "segmento", "dominio", "estado", "con_contrato",
                                                   "contrato_vigencia", "contrato_valor", "contrato_upgrade",
                                                   "contrato_permanencia")},
            "contactos": contactos, "servicios": servicios, "facturas": facturas,
        }
        return PLANTILLAS.TemplateResponse(request, "nuevo.html", _contexto(request, previo=previo, error=str(e)),
                                           status_code=400)
    return _volver(cuenta_id, ok=f"Cliente {cuenta_id} creado. Ya puedes escribirle a Care AI desde sus contactos.")


@app.post("/clientes/{cuenta_id}/contactos")
async def agregar_contacto(cuenta_id: str, correo: str = Form(""), nombre: str = Form(""),
                           autorizado: str = Form("")):
    try:
        c = sistemas.agregar_contacto(cuenta_id, correo, nombre, autorizado == "1")
    except sistemas.ErrorSistema as e:
        return _volver(cuenta_id, "crm", error=str(e))
    return _volver(cuenta_id, "crm", ok=f"Contacto {c} agregado.")


@app.post("/clientes/{cuenta_id}/editar")
async def editar_cuenta(cuenta_id: str, razon_social: str = Form(""), segmento: str = Form(""),
                        estado: str = Form(""), dominio: str = Form("")):
    try:
        sistemas.editar_cuenta(cuenta_id, razon_social=razon_social, segmento=segmento,
                               estado=estado, dominio=dominio)
    except sistemas.ErrorSistema as e:
        return _volver(cuenta_id, "crm", error=str(e))
    return _volver(cuenta_id, "crm", ok="Datos de la cuenta guardados.")


@app.post("/clientes/{cuenta_id}/contactos/editar")
async def editar_contacto(cuenta_id: str, correo_actual: str = Form(""), correo: str = Form(""),
                          nombre: str = Form(""), autorizado: str = Form("")):
    try:
        c = sistemas.editar_contacto(cuenta_id, correo_actual, correo=correo, nombre=nombre,
                                     autorizado=autorizado == "1")
    except sistemas.ErrorSistema as e:
        return _volver(cuenta_id, "crm", error=str(e))
    return _volver(cuenta_id, "crm", ok=f"Contacto {c} guardado.")


@app.post("/clientes/{cuenta_id}/contactos/eliminar")
async def eliminar_contacto(cuenta_id: str, correo: str = Form("")):
    try:
        sistemas.eliminar_contacto(correo)
    except sistemas.ErrorSistema as e:
        return _volver(cuenta_id, "crm", error=str(e))
    return _volver(cuenta_id, "crm", ok=f"Contacto {correo} eliminado.")


@app.post("/clientes/{cuenta_id}/servicios")
async def agregar_servicio(cuenta_id: str, sede: str = Form(""), producto: str = Form("Internet Dedicado"),
                           mbps: str = Form(""), nodo: str = Form("")):
    try:
        sid = sistemas.agregar_servicio(cuenta_id, sede, mbps, nodo, producto)
    except sistemas.ErrorSistema as e:
        return _volver(cuenta_id, "red", error=str(e))
    return _volver(cuenta_id, "red", ok=f"Servicio {sid} agregado.")


@app.post("/clientes/{cuenta_id}/facturas")
async def agregar_factura(cuenta_id: str, periodo: str = Form(""), valor: str = Form(""),
                          estado: str = Form("pendiente")):
    try:
        numero = sistemas.agregar_factura(cuenta_id, periodo, valor, estado)
    except sistemas.ErrorSistema as e:
        return _volver(cuenta_id, "facturacion", error=str(e))
    return _volver(cuenta_id, "facturacion", ok=f"Factura {numero} agregada.")


@app.post("/clientes/{cuenta_id}/contrato")
async def agregar_contrato(cuenta_id: str, vigencia: str = Form(""), valor: str = Form(""),
                           upgrade: str = Form(""), permanencia: str = Form("12")):
    try:
        numero = sistemas.agregar_contrato(cuenta_id, vigencia, valor, upgrade == "1", permanencia)
    except sistemas.ErrorSistema as e:
        return _volver(cuenta_id, "contrato", error=str(e))
    return _volver(cuenta_id, "contrato", ok=f"Contrato {numero} registrado.")


@app.post("/clientes/{cuenta_id}/eliminar")
async def eliminar_cliente(cuenta_id: str, confirmo: str = Form("")):
    if not confirmo:
        return _volver(cuenta_id)
    try:
        sistemas.eliminar_cuenta(cuenta_id)
    except sistemas.ErrorSistema as e:
        return _volver(cuenta_id, error=str(e))
    return _volver(ok=f"Cliente {cuenta_id} eliminado.")


# --------------------------------------------------------------------------- #
# Restaurar
# --------------------------------------------------------------------------- #
def _pedir_vaciado() -> int:
    """Le pide a la bandeja de Care AI que archive sus casos. Por HTTP a proposito:
    la consola no importa el codigo del agente, solo conoce su URL."""
    req = Request_(f"{BANDEJA_URL}/demo/vaciar", data=b"", method="POST")
    with urlopen(req, timeout=4) as r:
        return int(json.loads(r.read())["casos_archivados"])


@app.post("/restaurar")
async def restaurar(confirmo: str = Form(""), cuenta: str = Form(""), conservar: str = Form(""),
                    vaciar_bandeja: str = Form("")):
    """Sistemas en la semilla. Pide la casilla: se pierde lo que el copiloto escribio.

    Con `vaciar_bandeja` (facilidad de la POC para repetir pruebas) tambien archiva
    los casos de Care AI. Si la bandeja no contesta, los sistemas quedan
    restaurados igual y se avisa.
    """
    if not confirmo:
        return RedirectResponse(f"/?cuenta={cuenta}", status_code=303)
    sistemas.restaurar(conservar_altas=bool(conservar))
    params = {"cuenta": cuenta, "restaurado": 1}
    if vaciar_bandeja:
        try:
            params["bandeja"] = _pedir_vaciado()
        except (OSError, ValueError, KeyError):
            params["error"] = (f"No se pudo vaciar la bandeja de Care AI en {BANDEJA_URL} (esta arriba?). "
                               "Los sistemas si se restauraron.")
    return RedirectResponse(f"/?{urlencode(params)}", status_code=303)
