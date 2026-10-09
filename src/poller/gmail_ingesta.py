"""Ingesta real del buzon de Gmail.

Reemplaza el `CORREO` hardcodeado de `demo.py` por el buzon de verdad. Cada
mensaje se normaliza a un `CorreoEntrante` (ver `state.py`) y se entrega a
`servicio.procesar_correo`, que corre el grafo hasta el `interrupt` y deja el
caso en la bandeja.

    python -m src.poller.gmail_ingesta --autorizar     # una vez, abre el navegador
    python -m src.poller.gmail_ingesta                 # un barrido
    python -m src.poller.gmail_ingesta --loop 60       # barrido cada 60 s
    python -m src.poller.gmail_ingesta --dry-run       # normaliza e imprime, no corre el grafo

Por que la API de Gmail y no IMAP: da el `threadId` nativo, permite etiquetar lo
ya procesado, y el dia que el volumen justifique push por Pub/Sub solo cambia el
disparador, no `a_correo` ni el grafo.

**Idempotencia.** Tres capas, y hacen falta las tres:
  1. La query excluye lo ya etiquetado, para no bajar lo mismo cada minuto.
  2. `db.reservar` corta por Message-ID antes de arrancar el grafo, que es lo que
     salva de dos barridos solapados.
  3. El nodo `ingesta` vuelve a consultarlo, para quien entre por otra puerta.
La etiqueta sola no basta: marcarla puede fallar despues de procesar.

**Lo que no hace.** Adjuntos (se listan los nombres, no se descargan) ni hilos
multi-correo: cada mensaje abre su propio caso. Es el siguiente pliegue de
complejidad y el README explica por que va despues de tener la metrica base.
"""

import argparse
import base64
import os
import re
import sys
import time
from email.utils import parseaddr, parsedate_to_datetime
from html import unescape
from pathlib import Path
from typing import Any

from ..agente import servicio
from ..agente.db import RAIZ

SCOPE_ESCRITURA = "https://www.googleapis.com/auth/gmail.modify"
SCOPE_LECTURA = "https://www.googleapis.com/auth/gmail.readonly"

QUERY_POR_DEFECTO = "in:inbox is:unread"
ETIQUETA_POR_DEFECTO = "CareAI/Procesado"


def _ruta(var: str, defecto: str) -> Path:
    p = Path(os.getenv(var, defecto))
    return p if p.is_absolute() else RAIZ / p


def ruta_secreto() -> Path:
    """El secreto OAuth, se llame como se llame.

    Google no lo descarga como `credentials.json` sino como
    `client_secret_<id>.apps.googleusercontent.com.json`, y pedirle a alguien que
    renombre un archivo para que el programa lo encuentre es trabajo que el
    programa puede hacer solo. Con `GMAIL_CREDENTIALS` se fuerza uno concreto.
    """
    explicita = _ruta("GMAIL_CREDENTIALS", "credentials.json")
    if explicita.exists() or os.getenv("GMAIL_CREDENTIALS"):
        return explicita

    candidatos = sorted(RAIZ.glob("client_secret*.json"))
    if len(candidatos) == 1:
        return candidatos[0]
    if len(candidatos) > 1:
        # Dos secretos son dos proyectos de Google distintos: elegir por orden
        # alfabetico seria adivinar, y el error saldria mucho despues.
        raise SystemExit(
            "Hay varios client_secret*.json en la raiz:\n  "
            + "\n  ".join(c.name for c in candidatos)
            + "\n\nDeja solo uno, o indica cual en .env:  GMAIL_CREDENTIALS=<nombre>.json"
        )
    return explicita


def solo_lectura() -> bool:
    return os.getenv("GMAIL_SOLO_LECTURA", "").lower() in ("1", "true", "si")


def scopes() -> list[str]:
    return [SCOPE_LECTURA if solo_lectura() else SCOPE_ESCRITURA]


# --------------------------------------------------------------------------- #
# Normalizacion MIME -> CorreoEntrante
#
# Todo lo de esta seccion es funcion pura sobre el dict que devuelve la API. Eso
# permite probar el parseo con un mensaje de ejemplo y sin credenciales, que es
# donde de verdad aparecen los bugs de la ingesta.
# --------------------------------------------------------------------------- #
_ETIQUETAS_HTML = re.compile(r"<[^>]+>")
_BLOQUES_INVISIBLES = re.compile(r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_SALTOS = re.compile(r"<br\s*/?>|</p>|</div>|</tr>", re.IGNORECASE)
_LINEAS_VACIAS = re.compile(r"\n{3,}")


def html_a_texto(html: str) -> str:
    """Suficiente para correos de negocio. No es un parser de HTML y no pretende
    serlo: el destino es un prompt, no un render."""
    texto = _BLOQUES_INVISIBLES.sub(" ", html)
    texto = _SALTOS.sub("\n", texto)
    texto = _ETIQUETAS_HTML.sub("", texto)
    texto = unescape(texto)
    # strip y no rstrip: al quitar un <style> o un <script> queda un espacio suelto
    # que se convierte en sangria fantasma al principio de la linea siguiente.
    texto = "\n".join(linea.strip() for linea in texto.splitlines())
    return _LINEAS_VACIAS.sub("\n\n", texto).strip()


def _decodificar(parte: dict[str, Any]) -> str:
    datos = (parte.get("body") or {}).get("data")
    if not datos:
        return ""
    crudo = base64.urlsafe_b64decode(datos.encode("ascii"))
    juego = "utf-8"
    for h in parte.get("headers", []):
        if h.get("name", "").lower() == "content-type" and "charset=" in h.get("value", "").lower():
            juego = h["value"].lower().split("charset=")[1].split(";")[0].strip().strip('"') or "utf-8"
            break
    try:
        return crudo.decode(juego, errors="replace")
    except LookupError:  # charset que Python no conoce
        return crudo.decode("utf-8", errors="replace")


def _recolectar(parte: dict[str, Any], plano: list[str], html: list[str], adjuntos: list[str]) -> None:
    tipo = (parte.get("mimeType") or "").lower()
    nombre = parte.get("filename") or ""

    if nombre:
        adjuntos.append(nombre)
    elif tipo == "text/plain":
        plano.append(_decodificar(parte))
    elif tipo == "text/html":
        html.append(_decodificar(parte))

    for hija in parte.get("parts") or []:
        _recolectar(hija, plano, html, adjuntos)


def cuerpo_y_adjuntos(payload: dict[str, Any]) -> tuple[str, list[str]]:
    """text/plain si existe; si el remitente solo mando HTML, se degrada a texto."""
    plano: list[str] = []
    html: list[str] = []
    adjuntos: list[str] = []
    _recolectar(payload or {}, plano, html, adjuntos)

    texto = "\n".join(p for p in plano if p.strip())
    if not texto.strip() and html:
        texto = html_a_texto("\n".join(html))
    return texto.strip(), adjuntos


def _cabeceras(payload: dict[str, Any]) -> dict[str, str]:
    return {h.get("name", "").lower(): h.get("value", "") for h in (payload or {}).get("headers", [])}


def _fecha_iso(cab: dict[str, str], mensaje: dict[str, Any]) -> str:
    bruta = cab.get("date", "")
    if bruta:
        try:
            return parsedate_to_datetime(bruta).isoformat()
        except (TypeError, ValueError):
            pass
    interna = mensaje.get("internalDate")
    if interna:
        from datetime import datetime, timezone

        return datetime.fromtimestamp(int(interna) / 1000, timezone.utc).isoformat()
    return ""


_ETIQUETAS_PROPIAS = {"SENT", "DRAFT"}


def es_propio(mensaje: dict[str, Any], buzon: str = "") -> bool:
    """El mensaje lo escribio el propio buzon: la respuesta que el ejecutivo manda
    desde el borrador, o el borrador mismo.

    Hay que descartarlos antes del grafo porque la query no basta: una respuesta
    conserva el asunto ('Re: CARE-TEST ...') y cae en cualquier busqueda por
    asunto. Si ademas el buzon esta en CARE_CORREO_DEMO, el CRM la reconoce como
    cliente y abre un caso con la solucion como si fuera una solicitud nueva.
    """
    if _ETIQUETAS_PROPIAS & set(mensaje.get("labelIds") or []):
        return True
    remitente = parseaddr(_cabeceras(mensaje.get("payload", {})).get("from", ""))[1].lower()
    return bool(buzon) and remitente == buzon.lower()


def a_correo(mensaje: dict[str, Any]) -> dict[str, Any]:
    """Mensaje de la API (format=full) -> `CorreoEntrante`."""
    payload = mensaje.get("payload", {})
    cab = _cabeceras(payload)
    cuerpo, adjuntos = cuerpo_y_adjuntos(payload)

    # Sin Message-ID no hay idempotencia; el id de Gmail es el mejor sustituto y
    # tambien es estable.
    message_id = cab.get("message-id", "").strip() or f"gmail:{mensaje.get('id', '')}"

    return {
        "message_id": message_id,
        "thread_id": mensaje.get("threadId", ""),
        # Direccion pelada, no 'Nombre <dir>': `remitente` es la clave con la que
        # el CRM busca la cuenta, no un campo para mostrar.
        "remitente": parseaddr(cab.get("from", ""))[1] or cab.get("from", ""),
        "para": [parseaddr(d)[1] or d.strip() for d in cab.get("to", "").split(",") if d.strip()],
        "asunto": cab.get("subject", ""),
        "cuerpo": cuerpo,
        "recibido_en": _fecha_iso(cab, mensaje),
        "adjuntos": adjuntos,
    }


# --------------------------------------------------------------------------- #
# Cliente de la API
# --------------------------------------------------------------------------- #
def _credenciales(interactivo: bool):
    from google.auth.transport.requests import Request as PeticionOAuth
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    token = _ruta("GMAIL_TOKEN", "token.json")
    secreto = ruta_secreto()

    creds = Credentials.from_authorized_user_file(str(token), scopes()) if token.exists() else None

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(PeticionOAuth())
        token.write_text(creds.to_json(), encoding="utf-8")

    if creds and creds.valid:
        return creds

    if not interactivo:
        raise SystemExit(
            f"No hay autorizacion valida en {token.name}.\n"
            "Corre una vez:  python -m src.poller.gmail_ingesta --autorizar"
        )
    if not secreto.exists():
        raise SystemExit(
            f"Falta {secreto}.\n"
            "En console.cloud.google.com: habilita la Gmail API, crea un ID de cliente\n"
            "OAuth de tipo 'App de escritorio' y descarga el JSON con ese nombre."
        )

    creds = InstalledAppFlow.from_client_secrets_file(str(secreto), scopes()).run_local_server(port=0)
    token.write_text(creds.to_json(), encoding="utf-8")
    print(f"[gmail] autorizacion guardada en {token}")
    return creds


def cliente(interactivo: bool = False):
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=_credenciales(interactivo), cache_discovery=False)


def diagnostico() -> int:
    """Comprueba la conexion con Gmail y dice que falta, si falta algo.

    Antes de procesar un solo correo conviene saber tres cosas: que el token vale,
    contra que buzon estamos, y cuantos mensajes trae la query. Sin esto, el
    primer barrido real es a la vez la primera prueba de la autenticacion, y
    cuando falla no se sabe si fue el OAuth, la query o el grafo.
    """
    secreto = ruta_secreto()
    token = _ruta("GMAIL_TOKEN", "token.json")

    print("Archivos")
    print(f"  secreto OAuth     {'OK' if secreto.exists() else 'FALTA'}   {secreto.name}")
    print(f"  token             {'OK' if token.exists() else 'FALTA'}   {token.name}")
    if not secreto.exists():
        print("\nFalta el secreto OAuth. En console.cloud.google.com:")
        print("  1. Habilita la Gmail API")
        print("  2. Crea un ID de cliente OAuth de tipo 'App de escritorio'")
        print(f"  3. Descarga el JSON como {secreto.name} en la raiz del proyecto")
        return 1
    if not token.exists():
        print("\nFalta autorizar:  python -m src.poller.gmail_ingesta --autorizar")
        return 1

    try:
        svc = cliente()
        perfil = svc.users().getProfile(userId=_usuario()).execute()
    except Exception as exc:  # noqa: BLE001
        print(f"\nNo se pudo conectar: {exc}")
        print("Si habla de scope o de credenciales invalidas, borra token.json y reautoriza.")
        return 1

    print("\nConexion")
    print(f"  buzon             {perfil.get('emailAddress')}")
    print(f"  mensajes totales  {perfil.get('messagesTotal')}")
    print(f"  scope             {'solo lectura' if solo_lectura() else 'lectura + etiquetar + borradores'}")

    query = os.getenv("GMAIL_QUERY", QUERY_POR_DEFECTO)
    ids = buscar_ids(svc, query, int(os.getenv("GMAIL_MAX", "25")))
    print("\nQuery")
    print(f"  {query!r}")
    print(f"  {len(ids)} mensaje(s) entrarian en el proximo barrido")

    demo = os.getenv("CARE_CORREO_DEMO", "").strip()
    print("\nCRM simulado")
    if demo:
        print(f"  CARE_CORREO_DEMO = {demo}")
        print("  Los correos de esa direccion se reconocen como contacto de UFPSO.")
    else:
        print("  CARE_CORREO_DEMO vacio: todo correo saldra como NO IDENTIFICADA")
        print(f"  Ponla en .env con {perfil.get('emailAddress')} para ejercitar las validaciones.")

    print("\nListo. Siguiente:  python -m src.poller.gmail_ingesta --dry-run")
    return 0


def _usuario() -> str:
    return os.getenv("GMAIL_USER", "me")


def buscar_ids(svc, query: str, maximo: int) -> list[str]:
    """Pagina hasta `maximo`. Gmail devuelve 100 por pagina como mucho."""
    ids: list[str] = []
    pagina = None
    while len(ids) < maximo:
        resp = (
            svc.users()
            .messages()
            .list(userId=_usuario(), q=query, maxResults=min(100, maximo - len(ids)), pageToken=pagina)
            .execute()
        )
        ids += [m["id"] for m in resp.get("messages", [])]
        pagina = resp.get("nextPageToken")
        if not pagina:
            break
    return ids[:maximo]


def _id_etiqueta(svc, nombre: str) -> str | None:
    """Devuelve el id de la etiqueta, creandola si hace falta."""
    existentes = svc.users().labels().list(userId=_usuario()).execute().get("labels", [])
    for lab in existentes:
        if lab["name"] == nombre:
            return lab["id"]
    creada = (
        svc.users()
        .labels()
        .create(
            userId=_usuario(),
            body={"name": nombre, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
        )
        .execute()
    )
    return creada.get("id")


def crear_borrador(svc, correo: dict[str, Any], asunto: str, cuerpo: str) -> dict[str, Any]:
    """Deja un borrador de respuesta dentro del hilo original de Gmail.

    Tres detalles hacen que aparezca como respuesta y no como un correo suelto:
    `threadId` en la peticion, e `In-Reply-To` / `References` con el Message-ID
    del correo del cliente. Sin los headers, Gmail lo mete en el hilo pero otros
    clientes de correo lo muestran aparte.

    Crea, no envia: `drafts.create` entra en el scope gmail.modify que ya
    tenemos. Enviar exigiria gmail.send, que no pedimos a proposito.
    """
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["To"] = correo.get("remitente", "")
    msg["Subject"] = asunto if asunto.lower().startswith("re:") else f"Re: {asunto}"
    original = correo.get("message_id", "")
    if original.startswith("<"):        # un 'gmail:xxx' sintetico no sirve de ancla
        msg["In-Reply-To"] = original
        msg["References"] = original
    msg.set_content(cuerpo)

    peticion: dict[str, Any] = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")}
    if correo.get("thread_id"):
        peticion["threadId"] = correo["thread_id"]

    return svc.users().drafts().create(userId=_usuario(), body={"message": peticion}).execute()


ETIQUETA_CIERRE_POR_DEFECTO = "CareAI/Resuelto"


def etiquetar_hilo(svc, gmail_thread: str, nombre: str | None = None) -> str:
    """Marca el hilo del cliente como resuelto: es "dar el correo por terminado".

    Va sobre el hilo y no sobre el mensaje para que la respuesta que el ejecutivo
    mande desde el borrador quede tambien bajo la etiqueta.
    """
    nombre = nombre or os.getenv("GMAIL_LABEL_CIERRE", ETIQUETA_CIERRE_POR_DEFECTO)
    etiqueta = _id_etiqueta(svc, nombre)
    svc.users().threads().modify(
        userId=_usuario(), id=gmail_thread, body={"addLabelIds": [etiqueta]}
    ).execute()
    return nombre


def marcar(svc, gmail_id: str, etiqueta_id: str | None) -> None:
    cambios: dict[str, Any] = {"removeLabelIds": ["UNREAD"]}
    if etiqueta_id:
        cambios["addLabelIds"] = [etiqueta_id]
    svc.users().messages().modify(userId=_usuario(), id=gmail_id, body=cambios).execute()


# --------------------------------------------------------------------------- #
# Barrido
# --------------------------------------------------------------------------- #
def barrer(svc, query: str, maximo: int, marcar_leidos: bool, dry_run: bool) -> dict[str, int]:
    conteo = {"vistos": 0, "pendientes": 0, "duplicados": 0, "sin_caso": 0, "errores": 0, "propios": 0}

    ids = buscar_ids(svc, query, maximo)
    if not ids:
        print(f"[gmail] sin mensajes para q={query!r}")
        return conteo

    buzon = svc.users().getProfile(userId=_usuario()).execute().get("emailAddress", "")

    etiqueta_id = None
    if marcar_leidos and not dry_run:
        etiqueta_id = _id_etiqueta(svc, os.getenv("GMAIL_LABEL", ETIQUETA_POR_DEFECTO))

    for gmail_id in ids:
        conteo["vistos"] += 1
        mensaje = svc.users().messages().get(userId=_usuario(), id=gmail_id, format="full").execute()
        if es_propio(mensaje, buzon):
            # No se etiqueta: la etiqueta de procesado es para correos del cliente.
            conteo["propios"] += 1
            continue
        correo = a_correo(mensaje)

        if dry_run:
            print(f"  · {correo['asunto'][:58]:58} | {correo['remitente'][:34]:34} | "
                  f"{len(correo['cuerpo'])} car. | {correo['message_id']}")
            continue

        res = servicio.procesar_correo(correo)
        estado = res["estado"]
        conteo[{"pendiente": "pendientes", "duplicado": "duplicados",
                "sin_caso": "sin_caso", "error": "errores"}[estado]] += 1

        if estado == "pendiente":
            caso = res["caso"]
            print(f"  + {caso['caso_id']}  P{caso['prioridad']}  conf {caso['confianza_global']:.2f}  "
                  f"{len(caso['solicitudes'])} solic.  {correo['asunto'][:40]}")
        elif estado == "error":
            print(f"  ! {correo['message_id']}: {res['error']}", file=sys.stderr)
        else:
            print(f"  = {correo['message_id']} ({estado})")

        # Se etiqueta incluso el duplicado: si no, la query lo vuelve a traer en
        # cada barrido para descartarlo otra vez.
        if marcar_leidos and estado != "error":
            try:
                marcar(svc, gmail_id, etiqueta_id)
            except Exception as exc:  # noqa: BLE001 - un fallo al etiquetar no invalida el caso
                print(f"  ! no se pudo etiquetar {gmail_id}: {exc}", file=sys.stderr)

    return conteo


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingesta del buzon de Gmail hacia la bandeja Care")
    ap.add_argument("--autorizar", action="store_true", help="corre el flujo OAuth y sale")
    ap.add_argument("--diagnostico", action="store_true", help="comprueba la conexion y sale")
    ap.add_argument("--query", default=os.getenv("GMAIL_QUERY", QUERY_POR_DEFECTO),
                    help=f"busqueda de Gmail (por defecto {QUERY_POR_DEFECTO!r})")
    ap.add_argument("--max", type=int, default=int(os.getenv("GMAIL_MAX", "25")),
                    help="tope de mensajes por barrido")
    ap.add_argument("--loop", type=int, metavar="SEG", help="repetir cada SEG segundos")
    ap.add_argument("--dry-run", action="store_true", help="normaliza e imprime, no corre el grafo")
    ap.add_argument("--no-marcar", action="store_true", help="no marcar como leido ni etiquetar")
    args = ap.parse_args()

    if args.autorizar:
        cliente(interactivo=True)
        print("[gmail] listo. Comprueba la conexion:  python -m src.poller.gmail_ingesta --diagnostico")
        return

    if args.diagnostico:
        raise SystemExit(diagnostico())

    svc = cliente()
    marcar_leidos = not args.no_marcar and not solo_lectura()
    if solo_lectura():
        print("[gmail] GMAIL_SOLO_LECTURA activo: no se marca nada en el buzon.")

    while True:
        conteo = barrer(svc, args.query, args.max, marcar_leidos, args.dry_run)
        print(f"[gmail] {conteo['vistos']} visto(s): {conteo['pendientes']} a revision, "
              f"{conteo['duplicados']} duplicado(s), {conteo['sin_caso']} sin caso, "
              f"{conteo['errores']} error(es), {conteo['propios']} propio(s) ignorado(s).")
        if not args.loop:
            break
        try:
            time.sleep(args.loop)
        except KeyboardInterrupt:
            print("\n[gmail] detenido.")
            break


if __name__ == "__main__":
    main()
