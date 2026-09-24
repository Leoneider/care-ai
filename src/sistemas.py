"""Mini sistema legado de la POC: CRM, facturacion, contratos, red y mesa de servicio.

Reemplaza los diccionarios que vivian en `backends.py`. Es a proposito un sistema
*aparte* de la bandeja: su propia base (`data/sistemas.db`, o `CARE_SISTEMAS_DB`)
y su propio registro de operaciones. Hace el papel de cinco sistemas que en
produccion son de otros equipos, y vivir en otro archivo obliga a que el copiloto
lo toque solo a traves de `backends.py`, igual que tocaria los reales.

Tres cosas lo hacen util para demostrar:

- **Se puede escribir.** Las herramientas de la Fase 2 cambian datos de verdad:
  el ancho de banda de un servicio, la capacidad libre del nodo, una nota credito
  contra una factura, el estado de un ticket.
- **Todo cambio deja rastro** en `operacion`, con el antes y el despues. La
  pestana "Sistemas" de la bandeja lo pinta; es lo que convence en una demo.
- **Se restaura.** `restaurar()` vuelve a los datos sembrados para repetir la demo
  desde cero.

El sistema se defiende solo, como uno real: rechaza un upgrade que no cabe en el
nodo o una nota credito mayor que la factura, aunque el copiloto ya lo haya
validado antes. La validacion del grafo es para decidir; esta es para no romper.
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

RAIZ = Path(__file__).resolve().parent.parent
RUTA_POR_DEFECTO = "data/sistemas.db"

# Estados de ticket que cuentan como "abierto" para la mesa de servicio.
TICKET_ABIERTO = ("abierto", "en_proceso", "en_espera")


class ErrorSistema(Exception):
    """El sistema rechazo la operacion. El mensaje es para el ejecutivo."""


ESQUEMA = """
CREATE TABLE IF NOT EXISTS cuenta (
    cuenta_id    TEXT PRIMARY KEY,
    dominio      TEXT NOT NULL UNIQUE,
    razon_social TEXT NOT NULL,
    segmento     TEXT NOT NULL,
    estado       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contacto (
    correo     TEXT PRIMARY KEY,
    cuenta_id  TEXT NOT NULL REFERENCES cuenta(cuenta_id),
    nombre     TEXT NOT NULL DEFAULT '',
    autorizado INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS nodo (
    nodo                 TEXT PRIMARY KEY,
    capacidad_libre_mbps INTEGER NOT NULL,
    estado               TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS servicio (
    servicio_id      TEXT PRIMARY KEY,
    cuenta_id        TEXT NOT NULL REFERENCES cuenta(cuenta_id),
    producto         TEXT NOT NULL,
    sede             TEXT NOT NULL,
    ancho_banda_mbps INTEGER NOT NULL,
    nodo             TEXT NOT NULL REFERENCES nodo(nodo)
);
CREATE TABLE IF NOT EXISTS contrato (
    contrato                    TEXT PRIMARY KEY,
    cuenta_id                   TEXT NOT NULL UNIQUE REFERENCES cuenta(cuenta_id),
    vigencia_hasta              TEXT NOT NULL,
    valor_mensual_pactado       INTEGER NOT NULL,
    permite_upgrade_sin_adendum INTEGER NOT NULL,
    clausula_permanencia_meses  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS factura (
    factura   TEXT PRIMARY KEY,
    cuenta_id TEXT NOT NULL REFERENCES cuenta(cuenta_id),
    periodo   TEXT NOT NULL,              -- AAAA-MM
    valor     INTEGER NOT NULL,           -- lo facturado; las notas credito no lo tocan
    estado    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nota_credito (
    nota      TEXT PRIMARY KEY,
    factura   TEXT NOT NULL REFERENCES factura(factura),
    valor     INTEGER NOT NULL,
    motivo    TEXT NOT NULL,
    caso_id   TEXT NOT NULL DEFAULT '',
    creada_en TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ticket (
    ticket         TEXT PRIMARY KEY,
    cuenta_id      TEXT NOT NULL REFERENCES cuenta(cuenta_id),
    tema           TEXT NOT NULL,
    grupo          TEXT NOT NULL DEFAULT '',
    asunto         TEXT NOT NULL,
    estado         TEXT NOT NULL,
    caso_id        TEXT NOT NULL DEFAULT '',
    creado_en      TEXT NOT NULL,
    actualizado_en TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ticket_nota (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket    TEXT NOT NULL REFERENCES ticket(ticket),
    texto     TEXT NOT NULL,
    creada_en TEXT NOT NULL
);
-- Bitacora de todo lo que se escribe. Es lo que se muestra en la demo y lo que
-- responde "que cambio el copiloto en mis sistemas".
CREATE TABLE IF NOT EXISTS operacion (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    sistema    TEXT NOT NULL,
    operacion  TEXT NOT NULL,
    cuenta_id  TEXT NOT NULL DEFAULT '',
    caso_id    TEXT NOT NULL DEFAULT '',
    referencia TEXT NOT NULL DEFAULT '',
    antes      TEXT NOT NULL DEFAULT '{}',
    despues    TEXT NOT NULL DEFAULT '{}',
    detalle    TEXT NOT NULL DEFAULT ''
);
"""

# --------------------------------------------------------------------------- #
# Datos sembrados
# --------------------------------------------------------------------------- #
# ACME conserva los valores de siempre: las pruebas y el demo offline dependen de
# ellos. UFPSO es la cuenta del buzon real, preparada para el correo de la demo
# (ampliar Bogota a 500 Mbps + reclamo por la factura de septiembre): la sede
# existe, el nodo tiene capacidad, el contrato permite el upgrade y la factura de
# septiembre esta por encima de lo pactado. Todo el camino se puede ejecutar.
SEMILLA: dict[str, list[dict[str, Any]]] = {
    "cuenta": [
        {"cuenta_id": "B2B-004712", "dominio": "acme.com.co", "razon_social": "ACME Logistica S.A.S.",
         "segmento": "Corporativo", "estado": "activo"},
        {"cuenta_id": "B2B-005130", "dominio": "ufpso.edu.co", "razon_social": "UFPSO (cuenta de pruebas)",
         "segmento": "Educacion", "estado": "activo"},
    ],
    "contacto": [
        {"correo": "laura.gomez@acme.com.co", "cuenta_id": "B2B-004712", "nombre": "Laura Gomez", "autorizado": 1},
        {"correo": "compras@acme.com.co", "cuenta_id": "B2B-004712", "nombre": "Compras ACME", "autorizado": 1},
        # Las dos formas de la direccion: la que se pidio y la que de verdad
        # llego en el primer correo real.
        {"correo": "ltrigos@ufpso.edu.co", "cuenta_id": "B2B-005130", "nombre": "Leoneider Trigos", "autorizado": 1},
        {"correo": "ltrigosg@ufpso.edu.co", "cuenta_id": "B2B-005130", "nombre": "Leoneider Trigos", "autorizado": 1},
    ],
    "nodo": [
        {"nodo": "BOG-N14", "capacidad_libre_mbps": 800, "estado": "saludable"},
        {"nodo": "MED-N03", "capacidad_libre_mbps": 50, "estado": "saturado"},
        {"nodo": "BOG-N21", "capacidad_libre_mbps": 800, "estado": "saludable"},
        {"nodo": "CUC-N07", "capacidad_libre_mbps": 30, "estado": "saturado"},
        {"nodo": "OCA-N02", "capacidad_libre_mbps": 400, "estado": "saludable"},
    ],
    "servicio": [
        {"servicio_id": "SVC-8891", "cuenta_id": "B2B-004712", "producto": "Internet Dedicado",
         "sede": "Bogota - Calle 100", "ancho_banda_mbps": 200, "nodo": "BOG-N14"},
        {"servicio_id": "SVC-8892", "cuenta_id": "B2B-004712", "producto": "Internet Dedicado",
         "sede": "Medellin - Poblado", "ancho_banda_mbps": 100, "nodo": "MED-N03"},
        {"servicio_id": "SVC-9310", "cuenta_id": "B2B-005130", "producto": "Internet Dedicado",
         "sede": "Bogota - Sede Chapinero", "ancho_banda_mbps": 200, "nodo": "BOG-N21"},
        {"servicio_id": "SVC-9311", "cuenta_id": "B2B-005130", "producto": "Internet Dedicado",
         "sede": "Cucuta - Sede Centro", "ancho_banda_mbps": 100, "nodo": "CUC-N07"},
        {"servicio_id": "SVC-9312", "cuenta_id": "B2B-005130", "producto": "Internet Dedicado",
         "sede": "Ocana - Campus Algodonal", "ancho_banda_mbps": 300, "nodo": "OCA-N02"},
    ],
    "contrato": [
        {"contrato": "CTO-2231", "cuenta_id": "B2B-004712", "vigencia_hasta": "2027-03-31",
         "valor_mensual_pactado": 8_450_000, "permite_upgrade_sin_adendum": 1, "clausula_permanencia_meses": 12},
        {"contrato": "CTO-3107", "cuenta_id": "B2B-005130", "vigencia_hasta": "2027-06-30",
         "valor_mensual_pactado": 6_200_000, "permite_upgrade_sin_adendum": 1, "clausula_permanencia_meses": 24},
    ],
    "factura": [
        {"factura": "FE-90231", "cuenta_id": "B2B-004712", "periodo": "2026-08", "valor": 8_450_000, "estado": "pagada"},
        {"factura": "FE-91044", "cuenta_id": "B2B-004712", "periodo": "2026-09", "valor": 9_720_000, "estado": "pendiente"},
        {"factura": "FE-90877", "cuenta_id": "B2B-005130", "periodo": "2026-08", "valor": 6_200_000, "estado": "pagada"},
        {"factura": "FE-91502", "cuenta_id": "B2B-005130", "periodo": "2026-09", "valor": 7_150_000, "estado": "pendiente"},
    ],
    "ticket": [
        {"ticket": "INC-55120", "cuenta_id": "B2B-004712", "tema": "facturacion", "grupo": "Facturacion",
         "asunto": "Revision de cobros del periodo 2026-08", "estado": "abierto", "caso_id": "",
         "creado_en": "2026-09-02T14:10:00+00:00", "actualizado_en": "2026-09-02T14:10:00+00:00"},
    ],
}

_ORDEN_TABLAS = ("cuenta", "contacto", "nodo", "servicio", "contrato", "factura", "ticket")
_ORDEN_BORRADO = ("operacion", "ticket_nota", "ticket", "nota_credito", "factura",
                  "contrato", "servicio", "nodo", "contacto", "cuenta")


def ruta_db() -> Path:
    valor = os.getenv("CARE_SISTEMAS_DB", RUTA_POR_DEFECTO)
    p = Path(valor)
    return p if p.is_absolute() else RAIZ / p


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sembrar(con: sqlite3.Connection) -> None:
    for tabla in _ORDEN_TABLAS:
        for fila in SEMILLA.get(tabla, []):
            cols = ", ".join(fila)
            marcas = ", ".join("?" for _ in fila)
            con.execute(f"INSERT INTO {tabla} ({cols}) VALUES ({marcas})", tuple(fila.values()))


@contextmanager
def conexion() -> Iterator[sqlite3.Connection]:
    """Como `db.conexion`: una por operacion. La primera vez siembra los datos."""
    ruta = ruta_db()
    ruta.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(ruta, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        con.executescript(ESQUEMA)
        if con.execute("SELECT COUNT(*) FROM cuenta").fetchone()[0] == 0:
            _sembrar(con)
        yield con
        con.commit()
    finally:
        con.close()


def restaurar() -> None:
    """Vuelve a los datos sembrados. Borra la bitacora tambien: la demo empieza limpia."""
    with conexion() as con:
        for tabla in _ORDEN_BORRADO:
            con.execute(f"DELETE FROM {tabla}")
        con.execute("DELETE FROM sqlite_sequence")
        _sembrar(con)


def _registrar(con: sqlite3.Connection, sistema: str, operacion: str, *, cuenta_id: str = "",
               caso_id: str = "", referencia: str = "", antes: dict | None = None,
               despues: dict | None = None, detalle: str = "") -> None:
    con.execute(
        "INSERT INTO operacion (ts, sistema, operacion, cuenta_id, caso_id, referencia, antes, despues, detalle)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (_ahora(), sistema, operacion, cuenta_id, caso_id, referencia,
         json.dumps(antes or {}, ensure_ascii=False), json.dumps(despues or {}, ensure_ascii=False), detalle),
    )


def _uno(con: sqlite3.Connection, sql: str, *params: Any) -> dict[str, Any] | None:
    fila = con.execute(sql, params).fetchone()
    return dict(fila) if fila else None


# --------------------------------------------------------------------------- #
# CRM
# --------------------------------------------------------------------------- #
def cuenta_por_contacto(correo: str) -> tuple[dict[str, Any] | None, bool]:
    """(cuenta, autorizado) buscando la direccion exacta, como haria un CRM real."""
    with conexion() as con:
        contacto = _uno(con, "SELECT * FROM contacto WHERE correo = ?", correo.lower())
        if not contacto:
            return None, False
        cuenta = _uno(con, "SELECT * FROM cuenta WHERE cuenta_id = ?", contacto["cuenta_id"])
        return cuenta, bool(contacto["autorizado"])


def cuenta_por_dominio(dominio: str) -> dict[str, Any] | None:
    with conexion() as con:
        return _uno(con, "SELECT * FROM cuenta WHERE dominio = ?", dominio.lower())


def servicios(cuenta_id: str) -> list[dict[str, Any]]:
    with conexion() as con:
        return [
            dict(r) for r in con.execute(
                "SELECT servicio_id, producto, sede, ancho_banda_mbps, nodo FROM servicio"
                " WHERE cuenta_id = ? ORDER BY servicio_id", (cuenta_id,))
        ]


def servicio(servicio_id: str) -> dict[str, Any] | None:
    with conexion() as con:
        return _uno(con, "SELECT * FROM servicio WHERE servicio_id = ?", servicio_id)


# --------------------------------------------------------------------------- #
# Red
# --------------------------------------------------------------------------- #
def nodo(nodo_id: str) -> dict[str, Any] | None:
    with conexion() as con:
        return _uno(con, "SELECT capacidad_libre_mbps, estado FROM nodo WHERE nodo = ?", nodo_id)


def aplicar_ancho_banda(servicio_id: str, mbps: int, caso_id: str = "") -> dict[str, Any]:
    """Sube el ancho de banda del servicio y descuenta la diferencia del nodo."""
    with conexion() as con:
        svc = _uno(con, "SELECT * FROM servicio WHERE servicio_id = ?", servicio_id)
        if not svc:
            raise ErrorSistema(f"El servicio {servicio_id} no existe en el inventario de red.")
        nd = _uno(con, "SELECT * FROM nodo WHERE nodo = ?", svc["nodo"])
        delta = int(mbps) - svc["ancho_banda_mbps"]
        if delta <= 0:
            raise ErrorSistema(
                f"{servicio_id} ya tiene {svc['ancho_banda_mbps']} Mbps: {mbps} Mbps no es un aumento.")
        if nd["estado"] != "saludable" or nd["capacidad_libre_mbps"] < delta:
            raise ErrorSistema(
                f"El nodo {nd['nodo']} ({nd['estado']}, {nd['capacidad_libre_mbps']} Mbps libres)"
                f" no admite un aumento de {delta} Mbps.")

        con.execute("UPDATE servicio SET ancho_banda_mbps = ? WHERE servicio_id = ?", (int(mbps), servicio_id))
        con.execute("UPDATE nodo SET capacidad_libre_mbps = capacidad_libre_mbps - ? WHERE nodo = ?",
                    (delta, nd["nodo"]))
        _registrar(con, "RED", "aplicar_ancho_banda", cuenta_id=svc["cuenta_id"], caso_id=caso_id,
                   referencia=servicio_id,
                   antes={"ancho_banda_mbps": svc["ancho_banda_mbps"], "capacidad_libre_nodo": nd["capacidad_libre_mbps"]},
                   despues={"ancho_banda_mbps": int(mbps), "capacidad_libre_nodo": nd["capacidad_libre_mbps"] - delta},
                   detalle=f"{svc['sede']}: {svc['ancho_banda_mbps']} -> {mbps} Mbps en {nd['nodo']}")
        return {"servicio_id": servicio_id, "sede": svc["sede"], "nodo": nd["nodo"],
                "antes_mbps": svc["ancho_banda_mbps"], "despues_mbps": int(mbps),
                "capacidad_libre_nodo": nd["capacidad_libre_mbps"] - delta}


# --------------------------------------------------------------------------- #
# Contratos
# --------------------------------------------------------------------------- #
def contrato(cuenta_id: str) -> dict[str, Any]:
    with conexion() as con:
        fila = _uno(con, "SELECT * FROM contrato WHERE cuenta_id = ?", cuenta_id)
    if not fila:
        return {}
    fila["permite_upgrade_sin_adendum"] = bool(fila["permite_upgrade_sin_adendum"])
    fila.pop("cuenta_id", None)
    return fila


# --------------------------------------------------------------------------- #
# Facturacion
# --------------------------------------------------------------------------- #
def _con_ajustes(con: sqlite3.Connection, f: dict[str, Any]) -> dict[str, Any]:
    ajustes = con.execute("SELECT COALESCE(SUM(valor), 0) FROM nota_credito WHERE factura = ?",
                          (f["factura"],)).fetchone()[0]
    return {**f, "ajustes": int(ajustes), "valor_neto": int(f["valor"]) - int(ajustes)}


def facturas(cuenta_id: str) -> list[dict[str, Any]]:
    """`valor` es lo facturado; `valor_neto` descuenta las notas credito emitidas."""
    with conexion() as con:
        filas = [dict(r) for r in con.execute(
            "SELECT factura, periodo, valor, estado FROM factura WHERE cuenta_id = ? ORDER BY periodo",
            (cuenta_id,))]
        return [_con_ajustes(con, f) for f in filas]


def factura(numero: str) -> dict[str, Any] | None:
    with conexion() as con:
        f = _uno(con, "SELECT * FROM factura WHERE factura = ?", numero)
        return _con_ajustes(con, f) if f else None


def emitir_nota_credito(numero_factura: str, valor: int, motivo: str, caso_id: str = "") -> dict[str, Any]:
    with conexion() as con:
        f = _uno(con, "SELECT * FROM factura WHERE factura = ?", numero_factura)
        if not f:
            raise ErrorSistema(f"La factura {numero_factura} no existe.")
        f = _con_ajustes(con, f)
        if int(valor) <= 0:
            raise ErrorSistema("Una nota credito tiene que ser por un valor positivo.")
        if int(valor) > f["valor_neto"]:
            raise ErrorSistema(
                f"La nota credito ({int(valor):,} COP) supera el saldo de {numero_factura} ({f['valor_neto']:,} COP).")

        n = con.execute("SELECT COUNT(*) FROM nota_credito").fetchone()[0]
        nota = f"NC-{70001 + n}"
        con.execute("INSERT INTO nota_credito (nota, factura, valor, motivo, caso_id, creada_en) VALUES (?,?,?,?,?,?)",
                    (nota, numero_factura, int(valor), motivo, caso_id, _ahora()))
        _registrar(con, "FACTURACION", "emitir_nota_credito", cuenta_id=f["cuenta_id"], caso_id=caso_id,
                   referencia=numero_factura,
                   antes={"valor_neto": f["valor_neto"]},
                   despues={"valor_neto": f["valor_neto"] - int(valor), "nota_credito": nota},
                   detalle=f"{nota} por {int(valor):,} COP: {motivo}")
        return {"nota": nota, "factura": numero_factura, "valor": int(valor),
                "neto_antes": f["valor_neto"], "neto_despues": f["valor_neto"] - int(valor)}


def notas_credito(cuenta_id: str) -> list[dict[str, Any]]:
    with conexion() as con:
        return [dict(r) for r in con.execute(
            "SELECT n.* FROM nota_credito n JOIN factura f ON f.factura = n.factura"
            " WHERE f.cuenta_id = ? ORDER BY n.creada_en", (cuenta_id,))]


# --------------------------------------------------------------------------- #
# Mesa de servicio
# --------------------------------------------------------------------------- #
def tickets(cuenta_id: str, tema: str | None = None, solo_abiertos: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM ticket WHERE cuenta_id = ?"
    params: list[Any] = [cuenta_id]
    if tema:
        sql += " AND tema = ?"
        params.append(tema)
    if solo_abiertos:
        sql += f" AND estado IN ({','.join('?' for _ in TICKET_ABIERTO)})"
        params.extend(TICKET_ABIERTO)
    sql += " ORDER BY creado_en"
    with conexion() as con:
        return [dict(r) for r in con.execute(sql, params)]


def ticket(numero: str) -> dict[str, Any] | None:
    with conexion() as con:
        t = _uno(con, "SELECT * FROM ticket WHERE ticket = ?", numero)
        if t:
            t["notas"] = [dict(r) for r in con.execute(
                "SELECT texto, creada_en FROM ticket_nota WHERE ticket = ? ORDER BY id", (numero,))]
        return t


def crear_ticket(cuenta_id: str, tema: str, asunto: str, *, grupo: str = "Care",
                 estado: str = "en_proceso", caso_id: str = "") -> str:
    with conexion() as con:
        n = con.execute("SELECT COUNT(*) FROM ticket").fetchone()[0]
        numero = f"INC-{56001 + n}"
        ahora = _ahora()
        con.execute(
            "INSERT INTO ticket (ticket, cuenta_id, tema, grupo, asunto, estado, caso_id, creado_en, actualizado_en)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (numero, cuenta_id, tema, grupo, asunto, estado, caso_id, ahora, ahora))
        _registrar(con, "MESA", "crear_ticket", cuenta_id=cuenta_id, caso_id=caso_id, referencia=numero,
                   despues={"estado": estado, "grupo": grupo}, detalle=asunto)
        return numero


def actualizar_ticket(numero: str, *, estado: str | None = None, nota: str = "", caso_id: str = "") -> None:
    with conexion() as con:
        t = _uno(con, "SELECT * FROM ticket WHERE ticket = ?", numero)
        if not t:
            raise ErrorSistema(f"El ticket {numero} no existe.")
        ahora = _ahora()
        if nota:
            con.execute("INSERT INTO ticket_nota (ticket, texto, creada_en) VALUES (?,?,?)", (numero, nota, ahora))
        if estado and estado != t["estado"]:
            con.execute("UPDATE ticket SET estado = ?, actualizado_en = ? WHERE ticket = ?", (estado, ahora, numero))
            _registrar(con, "MESA", "cambiar_estado_ticket", cuenta_id=t["cuenta_id"], caso_id=caso_id,
                       referencia=numero, antes={"estado": t["estado"]}, despues={"estado": estado},
                       detalle=nota)
        elif nota:
            con.execute("UPDATE ticket SET actualizado_en = ? WHERE ticket = ?", (ahora, numero))
            _registrar(con, "MESA", "anotar_ticket", cuenta_id=t["cuenta_id"], caso_id=caso_id,
                       referencia=numero, detalle=nota)


# --------------------------------------------------------------------------- #
# Lectura para la pestana "Sistemas"
# --------------------------------------------------------------------------- #
def cuentas() -> list[dict[str, Any]]:
    with conexion() as con:
        return [dict(r) for r in con.execute("SELECT * FROM cuenta ORDER BY razon_social")]


def operaciones(cuenta_id: str | None = None, limite: int = 60) -> list[dict[str, Any]]:
    sql = "SELECT * FROM operacion"
    params: list[Any] = []
    if cuenta_id:
        sql += " WHERE cuenta_id = ?"
        params.append(cuenta_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limite)
    with conexion() as con:
        filas = [dict(r) for r in con.execute(sql, params)]
    for f in filas:
        antes, despues = json.loads(f["antes"]), json.loads(f["despues"])
        f["cambios"] = [
            {"campo": k, "antes": antes.get(k), "despues": despues.get(k)}
            for k in dict.fromkeys([*antes, *despues])
            if antes.get(k) != despues.get(k)
        ]
    return filas


def vista_cuenta(cuenta_id: str) -> dict[str, Any] | None:
    """Todo lo que la pestana "Sistemas" pinta de una cuenta, en una sola llamada."""
    with conexion() as con:
        c = _uno(con, "SELECT * FROM cuenta WHERE cuenta_id = ?", cuenta_id)
        if not c:
            return None
        contactos = [dict(r) for r in con.execute(
            "SELECT * FROM contacto WHERE cuenta_id = ? ORDER BY correo", (cuenta_id,))]
        svcs = [dict(r) for r in con.execute(
            "SELECT s.*, n.capacidad_libre_mbps, n.estado AS estado_nodo FROM servicio s"
            " JOIN nodo n ON n.nodo = s.nodo WHERE s.cuenta_id = ? ORDER BY s.servicio_id", (cuenta_id,))]
        tks = [dict(r) for r in con.execute(
            "SELECT * FROM ticket WHERE cuenta_id = ? ORDER BY creado_en DESC", (cuenta_id,))]
        for t in tks:
            t["notas"] = [dict(r) for r in con.execute(
                "SELECT texto, creada_en FROM ticket_nota WHERE ticket = ? ORDER BY id", (t["ticket"],))]
        modificadas = {r[0] for r in con.execute(
            "SELECT DISTINCT referencia FROM operacion WHERE cuenta_id = ?", (cuenta_id,))}
    ctr = contrato(cuenta_id)
    pactado = ctr.get("valor_mensual_pactado")
    fac = [{**f, "diferencia": (f["valor_neto"] - pactado) if pactado is not None else None}
           for f in facturas(cuenta_id)]
    return {
        "cuenta": c,
        "contactos": contactos,
        "servicios": svcs,
        "contrato": ctr,
        "facturas": fac,
        "notas_credito": notas_credito(cuenta_id),
        "tickets": tks,
        "modificadas": modificadas,
        "operaciones": operaciones(cuenta_id),
    }
