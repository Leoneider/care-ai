"""APARCADO: escrito pero NO conectado al grafo.

`aplicar_decision` sigue escribiendo feedback.jsonl a mano. Para conectarlo:
    1. nodes.py:  from . import backends, feedback, validaciones
    2. nodes.py:  reemplazar el bloque del open() por
                  feedback.guardar(caso, decision, state["correo"].get("message_id", ""))
    3. .env:      CARE_FEEDBACK_DSN=sqlite:///data/feedback.db
    4. conftest.py con un fixture que apunte el DSN a tmp_path, o pytest
       volvera a escribir en el dataset real.

Persistencia del dataset de revisiones.

Reemplaza el `open("feedback.jsonl", "a")` que estaba incrustado en el nodo
`aplicar_decision`. Tres cosas cambian con esto:

- La ruta deja de ser relativa al CWD. Antes, correr el demo desde otra carpeta
  creaba un feedback.jsonl distinto, y `pytest` escribia en el mismo archivo que
  las revisiones reales.
- El destino es configurable via CARE_FEEDBACK_DSN, asi que los tests apuntan a
  una BD temporal y el piloto a Postgres sin tocar el grafo.
- Las columnas por las que se filtra (tipo, confianza, decision, fecha) salen del
  JSON y quedan indexadas. Sacar una metrica pasa de recorrer el archivo entero
  en Python a ser una consulta.

El nodo solo llama `guardar(...)`; no sabe donde termina el registro. Es la misma
costura que ya usa `backends.py` para CRM y facturacion.
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

RAIZ = Path(__file__).resolve().parents[2]
DSN_POR_DEFECTO = "sqlite:///data/feedback.db"

ESQUEMA = """
CREATE TABLE IF NOT EXISTS revision (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    caso_id     TEXT NOT NULL,
    message_id  TEXT NOT NULL DEFAULT '',
    decision    TEXT NOT NULL,
    comentario  TEXT NOT NULL DEFAULT '',
    creado_en   TEXT NOT NULL,
    propuesto   TEXT NOT NULL,
    final       TEXT
);
CREATE INDEX IF NOT EXISTS ix_revision_caso     ON revision(caso_id);
CREATE INDEX IF NOT EXISTS ix_revision_creado   ON revision(creado_en);
CREATE INDEX IF NOT EXISTS ix_revision_decision ON revision(decision);

-- Una fila por solicitud. Las metricas del README (clasificacion, calibracion de
-- la confianza, deteccion de datos faltantes) son por solicitud, no por caso.
CREATE TABLE IF NOT EXISTS solicitud_revisada (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id       INTEGER NOT NULL REFERENCES revision(id) ON DELETE CASCADE,
    solicitud_id      TEXT    NOT NULL,
    tipo              TEXT    NOT NULL,
    confianza         REAL    NOT NULL,
    requiere_atencion INTEGER NOT NULL,
    n_faltantes       INTEGER NOT NULL,
    n_acciones        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sol_revision ON solicitud_revisada(revision_id);
CREATE INDEX IF NOT EXISTS ix_sol_tipo     ON solicitud_revisada(tipo);
"""


def dsn() -> str:
    return os.getenv("CARE_FEEDBACK_DSN", DSN_POR_DEFECTO)


def ruta_sqlite(cadena: str | None = None) -> Path:
    """Traduce sqlite:///... a una ruta, siempre relativa a la raiz del proyecto.

    Relativa a la raiz y no al CWD: de lo contrario el archivo aparece donde sea
    que se haya lanzado el proceso, que era justo el problema anterior.
    """
    cadena = cadena or dsn()
    resto = cadena[len("sqlite:///"):]
    p = Path(resto)
    return p if p.is_absolute() else RAIZ / p


@contextmanager
def conexion() -> Iterator[sqlite3.Connection]:
    cadena = dsn()
    if not cadena.startswith("sqlite:///"):
        # Postgres es el mismo SQL con marcadores %s en vez de ?; se agrega aqui
        # cuando haya servidor contra el cual probarlo.
        raise NotImplementedError(f"Backend no soportado todavia: {cadena.split(':')[0]}")

    ruta = ruta_sqlite(cadena)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(ruta, timeout=10)
    try:
        con.execute("PRAGMA journal_mode=WAL")    # varios workers escribiendo a la vez
        con.execute("PRAGMA foreign_keys=ON")
        con.executescript(ESQUEMA)
        yield con
        con.commit()
    finally:
        con.close()


def guardar(caso: dict[str, Any], decision: dict[str, Any], message_id: str = "") -> int:
    """Registra una revision y devuelve su id."""
    solicitudes = caso.get("solicitudes", [])
    final = decision.get("acciones_finales")

    with conexion() as con:
        cur = con.execute(
            "INSERT INTO revision (caso_id, message_id, decision, comentario, creado_en, propuesto, final) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                caso.get("caso_id", ""),
                message_id,
                decision.get("decision", ""),
                decision.get("comentario", ""),
                datetime.now(timezone.utc).isoformat(),
                json.dumps(solicitudes, ensure_ascii=False, default=str),
                json.dumps(final, ensure_ascii=False, default=str) if final else None,
            ),
        )
        rid = cur.lastrowid
        con.executemany(
            "INSERT INTO solicitud_revisada "
            "(revision_id, solicitud_id, tipo, confianza, requiere_atencion, n_faltantes, n_acciones) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    rid,
                    s.get("id", ""),
                    s.get("tipo", ""),
                    float(s.get("confianza", 0.0)),
                    int(bool(s.get("requiere_atencion"))),
                    len(s.get("faltantes", [])),
                    len(s.get("acciones", [])),
                )
                for s in solicitudes
            ],
        )
        return rid


def contar() -> tuple[int, int]:
    with conexion() as con:
        return (
            con.execute("SELECT COUNT(*) FROM revision").fetchone()[0],
            con.execute("SELECT COUNT(*) FROM solicitud_revisada").fetchone()[0],
        )


def podar(dias: int) -> int:
    """Borra revisiones mas viejas que `dias`. Devuelve cuantas borro.

    Ojo con usarla: el valor del dataset es acumular. Esto existe para depurar
    datos de prueba o cumplir una politica de retencion, no para ahorrar disco.
    """
    corte = datetime.now(timezone.utc).timestamp() - dias * 86400
    iso = datetime.fromtimestamp(corte, timezone.utc).isoformat()
    with conexion() as con:
        n = con.execute("DELETE FROM revision WHERE creado_en < ?", (iso,)).rowcount
        con.execute("DELETE FROM solicitud_revisada WHERE revision_id NOT IN (SELECT id FROM revision)")
        return n
