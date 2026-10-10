"""Bandeja de casos e idempotencia, en SQLite.

Este modulo aparece porque la UI y el poller de Gmail son procesos distintos.
Mientras todo corria en un solo proceso bastaba con estado en memoria; en cuanto
el correo entra por un lado y el ejecutivo decide por otro, hacen falta dos cosas
persistidas:

- **El checkpointer** (`SqliteSaver`, ver `graph.py`), para que el caso que quedo
  en `interrupt` siga ahi cuando el ejecutivo abra la bandeja horas despues.
- **Un indice de casos**, que es lo que vive aqui. El checkpointer sabe reanudar
  un thread, pero no responde "que casos hay pendientes, de que cuenta y con que
  prioridad" sin deserializar el estado completo de cada uno. La bandeja se pinta
  con una sola consulta a esta tabla.

La idempotencia usa la misma tabla: `message_id` es UNIQUE. Antes era un `set()`
en memoria, que con un poller real significa reprocesar el buzon entero en cada
reinicio.

Todo va a `data/care.db` (o `CARE_DB`), el mismo archivo que el checkpointer:
un archivo, un backup.
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

RAIZ = Path(__file__).resolve().parents[2]
RUTA_POR_DEFECTO = "data/care.db"

# Tres estados, no dos. `reservado` es la fila que el poller crea ANTES de correr
# el grafo para que un segundo barrido no tome el mismo correo; todavia no es un
# caso y no cuenta como procesada, o el propio nodo `ingesta` leeria como
# duplicado el correo que esta a punto de procesar.
RESERVADO = "reservado"
PENDIENTE = "pendiente"
# Fase 2: ya se ejecuto y verifico, pero algo pide la mirada del ejecutivo antes
# de cerrar. Cuenta como procesado: el correo no debe volver a entrar.
EN_VERIFICACION = "en_verificacion"
RESUELTO = "resuelto"
# Reinicio de la demo: el caso sale de todas las pestanas pero sigue contando como
# procesado. Borrarlo haria que el poller volviera a meter los correos CARE-TEST
# viejos del buzon, porque la query no excluye los ya etiquetados.
ARCHIVADO = "archivado"
PROCESADOS = (PENDIENTE, EN_VERIFICACION, RESUELTO, ARCHIVADO)
_OCULTOS = (RESERVADO, ARCHIVADO)

ESQUEMA = """
CREATE TABLE IF NOT EXISTS caso (
    thread_id     TEXT PRIMARY KEY,          -- thread de LangGraph, no el hilo de Gmail
    message_id    TEXT NOT NULL UNIQUE,      -- clave de idempotencia
    gmail_thread  TEXT NOT NULL DEFAULT '',
    caso_id       TEXT NOT NULL DEFAULT '',
    asunto        TEXT NOT NULL DEFAULT '',
    remitente     TEXT NOT NULL DEFAULT '',
    cuenta        TEXT NOT NULL DEFAULT '',
    razon_social  TEXT NOT NULL DEFAULT '',
    prioridad     INTEGER NOT NULL DEFAULT 3,
    confianza     REAL NOT NULL DEFAULT 0,
    n_solicitudes INTEGER NOT NULL DEFAULT 0,
    atencion      INTEGER NOT NULL DEFAULT 0,
    estado        TEXT NOT NULL DEFAULT 'reservado',
    decision      TEXT NOT NULL DEFAULT '',
    comentario    TEXT NOT NULL DEFAULT '',
    recibido_en   TEXT NOT NULL DEFAULT '',
    creado_en     TEXT NOT NULL,
    resuelto_en   TEXT
);
CREATE INDEX IF NOT EXISTS ix_caso_estado ON caso(estado, prioridad, creado_en);
"""

# `CREATE TABLE IF NOT EXISTS` no toca una tabla que ya existe, asi que las
# columnas agregadas despues del primer despliegue van aqui. Con una BD en uso,
# cambiar el esquema y confiar en el CREATE deja la tabla vieja y un
# "no such column" en la primera consulta.
COLUMNAS_NUEVAS = {
    "borrador_estado": "TEXT NOT NULL DEFAULT ''",
    "borrador_id": "TEXT NOT NULL DEFAULT ''",
    # Fase 2: como se cerro (automatico / ejecutivo:cerrar / ejecutivo:dejar_abierto)
    "cierre": "TEXT NOT NULL DEFAULT ''",
    "cierre_comentario": "TEXT NOT NULL DEFAULT ''",
    "etiqueta_cierre": "TEXT NOT NULL DEFAULT ''",
}


def _migrar(con: sqlite3.Connection) -> None:
    existentes = {fila["name"] for fila in con.execute("PRAGMA table_info(caso)")}
    for nombre, ddl in COLUMNAS_NUEVAS.items():
        if nombre not in existentes:
            con.execute(f"ALTER TABLE caso ADD COLUMN {nombre} {ddl}")


def ruta_db() -> Path:
    """Se resuelve en cada llamada, no al importar: asi el conftest puede apuntar
    CARE_DB a un tmp_path sin que el orden de los imports decida por nosotros."""
    valor = os.getenv("CARE_DB", RUTA_POR_DEFECTO)
    p = Path(valor)
    return p if p.is_absolute() else RAIZ / p


@contextmanager
def conexion() -> Iterator[sqlite3.Connection]:
    """Una conexion por operacion. SQLite abre en microsegundos y asi no hay que
    razonar sobre que thread de uvicorn toco cual handle."""
    ruta = ruta_db()
    ruta.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(ruta, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")  # el poller escribe mientras la UI lee
        con.executescript(ESQUEMA)
        _migrar(con)
        yield con
        con.commit()
    finally:
        con.close()


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Idempotencia
# --------------------------------------------------------------------------- #
def ya_procesado(message_id: str) -> bool:
    """Lo que consulta el nodo `ingesta`. Una reserva en vuelo no cuenta."""
    if not message_id:
        return False
    with conexion() as con:
        fila = con.execute(
            f"SELECT 1 FROM caso WHERE message_id = ? AND estado IN ({','.join('?' for _ in PROCESADOS)})",
            (message_id, *PROCESADOS),
        ).fetchone()
        return fila is not None


def reservar(message_id: str, thread_id: str) -> bool:
    """Marca el correo como visto y devuelve False si otro ya lo habia reservado.

    Se llama ANTES de correr el grafo, no despues: un poller que barre cada 60 s
    puede volver a ver el mismo correo mientras el anterior sigue en vuelo, y dos
    casos para el mismo Message-ID es justo lo que la idempotencia debe evitar.
    El precio es que un crash a mitad del tramo 1 deja el correo reservado sin
    caso util; `liberar()` existe para eso.
    """
    if not message_id:
        return True
    with conexion() as con:
        try:
            con.execute(
                "INSERT INTO caso (thread_id, message_id, estado, creado_en) VALUES (?, ?, ?, ?)",
                (thread_id, message_id, RESERVADO, _ahora()),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def liberar(message_id: str) -> None:
    """Deshace una reserva cuyo caso nunca llego a ensamblarse, para que el
    siguiente barrido lo vuelva a intentar. Nunca toca un caso ya creado."""
    with conexion() as con:
        con.execute("DELETE FROM caso WHERE message_id = ? AND estado = ?", (message_id, RESERVADO))


# --------------------------------------------------------------------------- #
# Bandeja
# --------------------------------------------------------------------------- #
def registrar_caso(thread_id: str, correo: dict[str, Any], caso: dict[str, Any]) -> None:
    """Completa la fila reservada con lo que el ejecutivo ve en la lista."""
    with conexion() as con:
        con.execute(
            """
            INSERT INTO caso (thread_id, message_id, gmail_thread, caso_id, asunto, remitente,
                              cuenta, razon_social, prioridad, confianza, n_solicitudes,
                              atencion, estado, recibido_en, creado_en)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(message_id) DO UPDATE SET
                thread_id     = excluded.thread_id,
                gmail_thread  = excluded.gmail_thread,
                caso_id       = excluded.caso_id,
                asunto        = excluded.asunto,
                remitente     = excluded.remitente,
                cuenta        = excluded.cuenta,
                razon_social  = excluded.razon_social,
                prioridad     = excluded.prioridad,
                confianza     = excluded.confianza,
                n_solicitudes = excluded.n_solicitudes,
                atencion      = excluded.atencion,
                estado        = excluded.estado,
                recibido_en   = excluded.recibido_en
            """,
            (
                thread_id,
                correo.get("message_id", ""),
                correo.get("thread_id", ""),
                caso.get("caso_id", ""),
                correo.get("asunto", ""),
                correo.get("remitente", ""),
                caso.get("cuenta", ""),
                caso.get("razon_social", ""),
                int(caso.get("prioridad", 3)),
                float(caso.get("confianza_global", 0.0)),
                len(caso.get("solicitudes", [])),
                int(bool(caso.get("requiere_atencion"))),
                PENDIENTE,
                correo.get("recibido_en", "") or "",
                _ahora(),
            ),
        )


def cerrar_caso(thread_id: str, decision: str, comentario: str = "",
               cierre: str = "", cierre_comentario: str = "") -> None:
    with conexion() as con:
        con.execute(
            "UPDATE caso SET estado=?, decision=?, comentario=?, resuelto_en=?, cierre=?, cierre_comentario=?"
            " WHERE thread_id=?",
            (RESUELTO, decision, comentario, _ahora(), cierre, cierre_comentario, thread_id),
        )


def marcar_en_verificacion(thread_id: str, decision: str, comentario: str = "") -> None:
    """La decision de la Fase 1 ya se tomo; falta el cierre del ejecutivo."""
    with conexion() as con:
        con.execute(
            "UPDATE caso SET estado=?, decision=?, comentario=? WHERE thread_id=?",
            (EN_VERIFICACION, decision, comentario, thread_id),
        )


def registrar_etiqueta(thread_id: str, estado: str) -> None:
    with conexion() as con:
        con.execute("UPDATE caso SET etiqueta_cierre=? WHERE thread_id=?", (estado, thread_id))


def registrar_borrador(thread_id: str, estado: str, draft_id: str = "") -> None:
    """Deja constancia del borrador de respuesta al cliente.

    Se persiste y no se devuelve solamente porque la UI redirige tras decidir: un
    borrador que solo existe en la respuesta del POST desaparece al recargar, y
    el ejecutivo no se entera de que tiene algo que revisar en Gmail.
    """
    with conexion() as con:
        con.execute(
            "UPDATE caso SET borrador_estado=?, borrador_id=? WHERE thread_id=?",
            (estado, draft_id, thread_id),
        )


def listar(estado: str | None = PENDIENTE, limite: int = 200) -> list[dict[str, Any]]:
    """Casos para la bandeja. Ordenados como los atiende un ejecutivo: primero la
    prioridad del tipo de solicitud, y dentro de ella el mas viejo."""
    sql = "SELECT * FROM caso WHERE estado NOT IN (?, ?)"
    params: list[Any] = list(_OCULTOS)
    if estado:
        sql += " AND estado = ?"
        params.append(estado)
    sql += " ORDER BY prioridad ASC, creado_en ASC LIMIT ?"
    params.append(limite)
    with conexion() as con:
        return [dict(r) for r in con.execute(sql, params)]


def obtener(thread_id: str) -> dict[str, Any] | None:
    with conexion() as con:
        fila = con.execute("SELECT * FROM caso WHERE thread_id = ?", (thread_id,)).fetchone()
        return dict(fila) if fila else None


def resumen() -> dict[str, int]:
    with conexion() as con:
        filas = con.execute(
            "SELECT estado, COUNT(*) n FROM caso WHERE estado NOT IN (?, ?) GROUP BY estado", _OCULTOS
        ).fetchall()
    conteo = {r["estado"]: r["n"] for r in filas}
    return {
        "pendientes": conteo.get(PENDIENTE, 0),
        "en_verificacion": conteo.get(EN_VERIFICACION, 0),
        "resueltos": conteo.get(RESUELTO, 0),
        "total": sum(conteo.values()),
    }


def marcar_procesado_directo(message_id: str) -> None:
    """Idempotencia para quien corre el grafo sin pasar por `servicio` (el demo,
    un test). Deja constancia del Message-ID sin fabricar un caso de bandeja."""
    if not message_id:
        return
    with conexion() as con:
        con.execute(
            "INSERT INTO caso (thread_id, message_id, estado, creado_en, resuelto_en, decision) "
            "VALUES (?,?,?,?,?,'directo') ON CONFLICT(message_id) DO NOTHING",
            (f"directo-{message_id}", message_id, RESUELTO, _ahora(), _ahora()),
        )


def archivar_todo() -> int:
    """Saca de la bandeja todos los casos sin perder la idempotencia. Devuelve cuantos."""
    with conexion() as con:
        cur = con.execute("UPDATE caso SET estado = ? WHERE estado IN (?, ?, ?)",
                          (ARCHIVADO, PENDIENTE, EN_VERIFICACION, RESUELTO))
        return cur.rowcount


def limpiar() -> None:
    """Solo para pruebas."""
    with conexion() as con:
        con.execute("DELETE FROM caso")
