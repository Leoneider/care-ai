"""Consola del mini sistema legado: CRM, facturacion, contratos, red y mesa de servicio.

Es una aplicacion aparte de la bandeja de Care AI, con su propio proceso y puerto:

    python -m src.sistemas_web        # http://127.0.0.1:8001

En produccion estos cinco sistemas son de otros equipos y tienen sus propias
pantallas. La POC los separa igual para que la demo cuente la historia correcta:
el agente es una cosa y el sistema es otra. Care AI escribe aqui solo a traves de
`backends.py`, y esta consola solo lee `sistemas.py`: no importa `db` ni
`servicio`, no sabe que hay una bandeja ni un grafo.

Por eso "Restaurar" solo devuelve los sistemas a la semilla. Vaciar la bandeja es
cosa de Care AI (`.\\care.cmd reiniciar` hace las dos).
"""

from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import sistemas

PLANTILLAS = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# La cuenta de pruebas del buzon real, que es la de la demo.
CUENTA_DEMO = "B2B-005130"


def _fecha(iso: str | None) -> str:
    """ISO 8601 -> 'dd/mm HH:MM'."""
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

app = FastAPI(title="Sistemas legados - POC", docs_url=None, redoc_url=None)


@app.get("/", response_class=HTMLResponse)
def ver_cuenta(request: Request, cuenta: str | None = None, restaurado: int = 0):
    cuentas = sistemas.cuentas()
    ids = [c["cuenta_id"] for c in cuentas]
    elegida = cuenta if cuenta in ids else (CUENTA_DEMO if CUENTA_DEMO in ids else (ids[0] if ids else None))
    return PLANTILLAS.TemplateResponse(
        request, "cuenta.html",
        {
            "request": request,
            "cuentas": cuentas,
            "elegida": elegida,
            "datos": sistemas.vista_cuenta(elegida) if elegida else None,
            "restaurado": bool(restaurado),
        },
    )


@app.post("/restaurar")
async def restaurar(confirmo: str = Form(""), cuenta: str = Form("")):
    """Sistemas en la semilla. Pide la casilla: se pierde lo que el copiloto escribio."""
    if not confirmo:
        return RedirectResponse(f"/?cuenta={cuenta}", status_code=303)
    sistemas.restaurar()
    return RedirectResponse(f"/?cuenta={cuenta}&restaurado=1", status_code=303)
