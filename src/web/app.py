"""UI del ejecutivo Care: bandeja, detalle del caso, y aprobar / editar / rechazar.

Fase 2: tambien la revision de cierre (pestana "En verificacion") y la pestana
"Sistemas", que muestra el mini sistema legado y lo que el copiloto cambio en el.

HTML renderizado en servidor, sin JS ni build. La UI no sabe que existe un grafo:
habla con `servicio.py`, que es quien traduce a `get_state` y `Command(resume=...)`.

    python -m src.web            # http://127.0.0.1:8000

Tres decisiones de esta capa:

- **La edicion se detecta, no se declara.** Si el ejecutivo desmarca una accion o
  cambia un parametro y pulsa "Aprobar", la decision se registra como `editar`
  con las acciones finales. Un boton que dice aprobar guardando algo distinto de
  lo propuesto arruina justo la metrica que el feedback existe para medir:
  cuantos casos pasan sin que el humano toque nada.
- **Solo se envia `acciones_finales` cuando hubo cambio.** `aprobar` a secas deja
  el campo nulo, que es lo que `aplicar_decision` entiende como "ejecuta lo
  propuesto".
- **Sin autenticacion.** Es una POC de escritorio, atada a 127.0.0.1. Antes de
  exponerla hace falta login y registrar quien decidio cada caso.
"""

from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import db, servicio, sistemas

PLANTILLAS = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _fecha(iso: str | None) -> str:
    """ISO 8601 -> 'dd/mm HH:MM'. La bandeja se lee de un vistazo; el ano completo
    y los microsegundos solo estorban."""
    if not iso:
        return "-"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%d/%m %H:%M")
    except ValueError:
        return iso[:16]


def _cop(valor: Any) -> str:
    """7150000 -> '7.150.000', como se escriben los pesos."""
    try:
        return f"{int(valor):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "-"


PLANTILLAS.env.filters["fecha"] = _fecha
PLANTILLAS.env.filters["cop"] = _cop

app = FastAPI(title="Care AI - Bandeja del ejecutivo", docs_url=None, redoc_url=None)


def _contexto(request: Request, **extra: Any) -> dict[str, Any]:
    return {"request": request, "conteo": servicio.resumen(), **extra}


@app.get("/", response_class=HTMLResponse)
def bandeja(request: Request):
    return PLANTILLAS.TemplateResponse(
        request, "bandeja.html",
        _contexto(request, vista="pendientes", casos=servicio.listar(db.PENDIENTE)),
    )


@app.get("/verificacion", response_class=HTMLResponse)
def en_verificacion(request: Request):
    return PLANTILLAS.TemplateResponse(
        request, "bandeja.html",
        _contexto(request, vista="verificacion", casos=servicio.listar(db.EN_VERIFICACION)),
    )


@app.get("/resueltos", response_class=HTMLResponse)
def resueltos(request: Request):
    return PLANTILLAS.TemplateResponse(
        request, "bandeja.html",
        _contexto(request, vista="resueltos", casos=servicio.listar(db.RESUELTO)),
    )


_VISTA_POR_ETAPA = {"revision": "pendientes", "verificacion": "verificacion", "cerrado": "resueltos"}


def _render_caso(request: Request, det: dict[str, Any], error: str | None = None, form: Any = None):
    """Pinta el detalle. Con `form`, repinta lo que el ejecutivo habia dejado.

    Un rechazo de validacion que ademas le borra las ediciones convierte el
    guardarrail en un castigo; `sel_previa` y `valores` devuelven el formulario
    tal como lo envio.
    """
    return PLANTILLAS.TemplateResponse(
        request,
        "caso.html",
        _contexto(
            request,
            vista=_VISTA_POR_ETAPA.get(det.get("etapa"), "pendientes"),
            error=error,
            comentario_previo=(form.get("comentario", "") if form else ""),
            confirmado=(form.get("confirmo_bloqueo") is not None if form else False),
            sel_previa=(set(form.getlist("sel")) if form else None),
            valores=(dict(form) if form else {}),
            bloqueantes=bloqueantes_de(det["caso"]),
            **det,
        ),
    )


@app.get("/caso/{thread_id}", response_class=HTMLResponse)
def ver_caso(request: Request, thread_id: str):
    det = servicio.detalle(thread_id)
    if det is None:
        return RedirectResponse("/", status_code=303)
    return _render_caso(request, det)


# --------------------------------------------------------------------------- #
# Decision
# --------------------------------------------------------------------------- #
def _accion_editada(form: Any, sid: str, idx: int, propuesta: dict[str, Any]) -> dict[str, Any]:
    """Reconstruye una accion con lo que el ejecutivo dejo en el formulario.

    Solo se tocan `descripcion` y `parametros`: el `codigo` y el `sistema` no son
    editables a proposito, porque el catalogo cerrado de `prompts.py` es el
    guardarrail y una UI que deje escribir codigos libres lo anula.
    """
    return {
        **propuesta,
        "descripcion": form.get(f"desc::{sid}::{idx}", propuesta.get("descripcion", "")),
        "parametros": {
            k: form.get(f"par::{sid}::{idx}::{k}", v)
            for k, v in (propuesta.get("parametros") or {}).items()
        },
    }


def _leer_acciones(form: Any, caso: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """Devuelve (acciones finales, hubo_cambio) comparando el form con lo propuesto."""
    marcadas = set(form.getlist("sel"))
    finales: list[dict[str, Any]] = []
    cambio = False

    for s in caso.get("solicitudes", []):
        sid = s["id"]
        for idx, propuesta in enumerate(s.get("acciones", [])):
            if f"{sid}::{idx}" not in marcadas:
                cambio = True  # el ejecutivo la descarto
                continue
            editada = _accion_editada(form, sid, idx, propuesta)
            if editada != propuesta:
                cambio = True
            finales.append(editada)

    return finales, cambio


def bloqueantes_de(caso: dict[str, Any]) -> list[dict[str, Any]]:
    """Validaciones que el codigo marco como bloqueantes.

    Son del caso, no de una accion: que el ejecutivo quite la accion conflictiva
    no hace que el nodo deje de estar saturado.
    """
    return [
        v
        for s in caso.get("solicitudes", [])
        for v in s.get("validaciones", [])
        if v.get("bloqueante")
    ]


@app.post("/caso/{thread_id}/decidir")
async def decidir(request: Request, thread_id: str, decision: str = Form(...), comentario: str = Form("")):
    det = servicio.detalle(thread_id)
    if det is None or not det["pendiente"]:
        return RedirectResponse(f"/caso/{thread_id}", status_code=303)

    comentario = comentario.strip()
    if decision == "rechazar":
        servicio.resolver(thread_id, "rechazar", comentario)
        return RedirectResponse(f"/caso/{thread_id}", status_code=303)

    # `await request.form()` en vez de parametros tipados: los nombres de los
    # campos de edicion dependen del caso (una accion, un parametro), asi que no
    # hay firma fija que declarar.
    form = await request.form()
    finales, hubo_cambio = _leer_acciones(form, det["caso"])

    # Friccion, no prohibicion: con una validacion bloqueante el ejecutivo sigue
    # pudiendo aprobar, pero tiene que decir que lo hace a sabiendas y por que.
    # Sin esto, la pildora roja compite con 40 casos en la bandeja y pierde: se
    # aprueba por inercia un upgrade a un nodo saturado. El motivo escrito es
    # ademas lo que despues permite ver si el bloqueo estaba mal calibrado.
    if bloqueantes_de(det["caso"]):
        falta_casilla = form.get("confirmo_bloqueo") is None
        if falta_casilla or not comentario:
            motivos = []
            if falta_casilla:
                motivos.append("marca la casilla de confirmacion")
            if not comentario:
                motivos.append("escribe el motivo en el comentario")
            return _render_caso(
                request,
                det,
                error="Este caso tiene una validacion bloqueante: " + " y ".join(motivos) + ".",
                form=form,
            )

    if decision == "editar" or hubo_cambio:
        servicio.resolver(thread_id, "editar", comentario, acciones_finales=finales)
    else:
        servicio.resolver(thread_id, "aprobar", comentario)

    return RedirectResponse(f"/caso/{thread_id}", status_code=303)


# --------------------------------------------------------------------------- #
# Fase 2: revision de cierre
# --------------------------------------------------------------------------- #
@app.post("/caso/{thread_id}/cerrar")
async def cerrar(request: Request, thread_id: str, decision: str = Form(...), comentario: str = Form("")):
    det = servicio.detalle(thread_id)
    if det is None or det["etapa"] != "verificacion":
        return RedirectResponse(f"/caso/{thread_id}", status_code=303)
    comentario = comentario.strip()
    # Dejar el ticket abierto es no dar por terminado el correo: que quede escrito
    # por que, o dentro de una semana nadie sabra que se esperaba.
    if decision == "dejar_abierto" and not comentario:
        return _render_caso(request, det, error="Para dejar el ticket abierto, escribe en el comentario que queda pendiente.")
    servicio.cerrar(thread_id, decision, comentario)
    return RedirectResponse(f"/caso/{thread_id}", status_code=303)


# --------------------------------------------------------------------------- #
# Pestana "Sistemas": el mini sistema legado
# --------------------------------------------------------------------------- #
@app.get("/sistemas", response_class=HTMLResponse)
def ver_sistemas(request: Request, cuenta: str | None = None, reiniciado: int = -1):
    cuentas = sistemas.cuentas()
    ids = [c["cuenta_id"] for c in cuentas]
    # Por defecto la cuenta de pruebas del buzon real, que es la de la demo.
    elegida = cuenta if cuenta in ids else ("B2B-005130" if "B2B-005130" in ids else (ids[0] if ids else None))
    return PLANTILLAS.TemplateResponse(
        request, "sistemas.html",
        _contexto(
            request,
            vista="sistemas",
            cuentas=cuentas,
            elegida=elegida,
            datos=sistemas.vista_cuenta(elegida) if elegida else None,
            reiniciado=reiniciado,
        ),
    )


@app.post("/demo/reiniciar")
async def reiniciar_demo(confirmo: str = Form(""), cuenta: str = Form("")):
    """Bandeja vacia y sistemas en la semilla. Pide la casilla: se pierde lo hecho."""
    if not confirmo:
        return RedirectResponse(f"/sistemas?cuenta={cuenta}", status_code=303)
    r = servicio.reiniciar_demo()
    return RedirectResponse(f"/sistemas?cuenta={cuenta}&reiniciado={r['casos_archivados']}", status_code=303)
