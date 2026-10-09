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

RAIZ = Path(__file__).resolve().parents[2]
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
-- Lo que se dio de alta a mano desde la consola (clientes de prueba, contactos,
-- servicios, facturas). Vive aparte de la semilla para que `restaurar()` lo
-- vuelva a sembrar en su estado inicial en vez de perderlo.
CREATE TABLE IF NOT EXISTS alta (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tabla      TEXT NOT NULL,
    cuenta_id  TEXT NOT NULL,
    referencia TEXT NOT NULL,
    fila       TEXT NOT NULL
);
"""

# --------------------------------------------------------------------------- #
# Datos sembrados
# --------------------------------------------------------------------------- #
# Una sola cuenta, UFPSO: la del buzon real, preparada para el correo de la demo
# (ampliar Bogota a 500 Mbps + reclamo por la factura de septiembre): la sede
# existe, el nodo tiene capacidad, el contrato permite el upgrade y la factura de
# septiembre esta por encima de lo pactado. Todo el camino se puede ejecutar.
# Cucuta corre sobre un nodo saturado: es el caso de la validacion bloqueante.
SEMILLA: dict[str, list[dict[str, Any]]] = {
    "cuenta": [
        {"cuenta_id": "B2B-005130", "dominio": "ufpso.edu.co", "razon_social": "UFPSO (cuenta de pruebas)",
         "segmento": "Educacion", "estado": "activo"},
    ],
    "contacto": [
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
        {"servicio_id": "SVC-9310", "cuenta_id": "B2B-005130", "producto": "Internet Dedicado",
         "sede": "Bogota - Sede Chapinero", "ancho_banda_mbps": 200, "nodo": "BOG-N21"},
        {"servicio_id": "SVC-9311", "cuenta_id": "B2B-005130", "producto": "Internet Dedicado",
         "sede": "Cucuta - Sede Centro", "ancho_banda_mbps": 100, "nodo": "CUC-N07"},
        {"servicio_id": "SVC-9312", "cuenta_id": "B2B-005130", "producto": "Internet Dedicado",
         "sede": "Ocana - Campus Algodonal", "ancho_banda_mbps": 300, "nodo": "OCA-N02"},
    ],
    "contrato": [
        {"contrato": "CTO-3107", "cuenta_id": "B2B-005130", "vigencia_hasta": "2027-06-30",
         "valor_mensual_pactado": 6_200_000, "permite_upgrade_sin_adendum": 1, "clausula_permanencia_meses": 24},
    ],
    "factura": [
        {"factura": "FE-90877", "cuenta_id": "B2B-005130", "periodo": "2026-08", "valor": 6_200_000, "estado": "pagada"},
        {"factura": "FE-91502", "cuenta_id": "B2B-005130", "periodo": "2026-09", "valor": 7_150_000, "estado": "pendiente"},
    ],
    "ticket": [],
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


def _insertar(con: sqlite3.Connection, tabla: str, fila: dict[str, Any]) -> None:
    cols = ", ".join(fila)
    marcas = ", ".join("?" for _ in fila)
    con.execute(f"INSERT INTO {tabla} ({cols}) VALUES ({marcas})", tuple(fila.values()))


def _sembrar(con: sqlite3.Connection) -> None:
    for tabla in _ORDEN_TABLAS:
        for fila in SEMILLA.get(tabla, []):
            _insertar(con, tabla, fila)


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


def restaurar(conservar_altas: bool = True) -> None:
    """Vuelve a los datos sembrados. Borra la bitacora tambien: la demo empieza limpia.

    Con `conservar_altas` (lo normal) los clientes creados desde la consola vuelven
    a su estado inicial en vez de desaparecer; sin el, queda solo la semilla.
    """
    with conexion() as con:
        for tabla in _ORDEN_BORRADO:
            con.execute(f"DELETE FROM {tabla}")
        if not conservar_altas:
            con.execute("DELETE FROM alta")
        con.execute("DELETE FROM sqlite_sequence WHERE name != 'alta'")
        _sembrar(con)
        altas = [dict(r) for r in con.execute("SELECT tabla, fila FROM alta ORDER BY id")]
        for tabla in _ORDEN_TABLAS:
            for a in altas:
                if a["tabla"] == tabla:
                    _insertar(con, tabla, json.loads(a["fila"]))


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
# Altas desde la consola: clientes de prueba
# --------------------------------------------------------------------------- #
# Con dos cuentas sembradas solo se puede probar con dos buzones. Estas funciones
# dejan dar de alta clientes con sus propios correos, servicios y facturas, para
# ensayar otros casos (nodo saturado, factura que cuadra, contacto no autorizado)
# sin tocar la semilla. No pasan por la bitacora: no son operaciones del copiloto,
# son datos de partida, y marcarlas como "modificadas" confundiria la demo.

ESTADOS_FACTURA = ("pendiente", "pagada", "vencida")
SEGMENTOS = ("Corporativo", "Empresarial", "Pyme", "Gobierno", "Educacion")
ESTADOS_CUENTA = ("activo", "suspendido")

# TLD reservado (RFC 2606): nunca coincide con un remitente real, asi que una
# cuenta sin dominio corporativo solo se encuentra por el correo exacto.
_SIN_DOMINIO = ".invalid"


def _siguiente(con: sqlite3.Connection, tabla: str, col: str, prefijo: str, ancho: int, minimo: int) -> str:
    """Siguiente identificador libre con el formato de la casa: B2B-005131, SVC-9313..."""
    mayor = minimo - 1
    for (valor,) in con.execute(f"SELECT {col} FROM {tabla} WHERE {col} LIKE ?", (f"{prefijo}%",)):
        cola = valor[len(prefijo):]
        if cola.isdigit():
            mayor = max(mayor, int(cola))
    return f"{prefijo}{mayor + 1:0{ancho}d}"


def _alta(con: sqlite3.Connection, tabla: str, cuenta_id: str, referencia: str, fila: dict[str, Any]) -> None:
    _insertar(con, tabla, fila)
    con.execute("INSERT INTO alta (tabla, cuenta_id, referencia, fila) VALUES (?,?,?,?)",
                (tabla, cuenta_id, referencia, json.dumps(fila, ensure_ascii=False)))


def _entero(valor: Any, que: str, minimo: int = 0) -> int:
    try:
        n = int(str(valor).replace(".", "").replace(",", "").strip())
    except (TypeError, ValueError):
        raise ErrorSistema(f"{que}: '{valor}' no es un numero.") from None
    if n < minimo:
        raise ErrorSistema(f"{que} tiene que ser al menos {minimo}.")
    return n


def _correo_valido(correo: str) -> str:
    correo = (correo or "").strip().lower()
    if correo.count("@") != 1 or not correo.split("@")[0] or "." not in correo.split("@")[1]:
        raise ErrorSistema(f"'{correo}' no es un correo valido.")
    return correo


def _alta_contacto(con: sqlite3.Connection, cuenta_id: str, correo: str, nombre: str, autorizado: bool) -> str:
    correo = _correo_valido(correo)
    duenio = _uno(con, "SELECT cuenta_id FROM contacto WHERE correo = ?", correo)
    if duenio:
        raise ErrorSistema(f"El correo {correo} ya es contacto de la cuenta {duenio['cuenta_id']}.")
    _alta(con, "contacto", cuenta_id, correo, {"correo": correo, "cuenta_id": cuenta_id,
                                               "nombre": (nombre or "").strip(), "autorizado": int(bool(autorizado))})
    return correo


def _alta_servicio(con: sqlite3.Connection, cuenta_id: str, producto: str, sede: str, mbps: Any, nodo: str) -> str:
    if not (sede or "").strip():
        raise ErrorSistema("Cada servicio necesita una sede.")
    if not _uno(con, "SELECT nodo FROM nodo WHERE nodo = ?", nodo):
        raise ErrorSistema(f"El nodo '{nodo}' no existe en el inventario de red.")
    sid = _siguiente(con, "servicio", "servicio_id", "SVC-", 4, 9400)
    _alta(con, "servicio", cuenta_id, sid, {
        "servicio_id": sid, "cuenta_id": cuenta_id, "producto": (producto or "Internet Dedicado").strip(),
        "sede": sede.strip(), "ancho_banda_mbps": _entero(mbps, "El ancho de banda", 1), "nodo": nodo})
    return sid


def _alta_factura(con: sqlite3.Connection, cuenta_id: str, periodo: str, valor: Any, estado: str) -> str:
    periodo = (periodo or "").strip()
    if len(periodo) != 7 or periodo[4] != "-" or not (periodo[:4] + periodo[5:]).isdigit() \
            or not 1 <= int(periodo[5:]) <= 12:
        raise ErrorSistema(f"El periodo '{periodo}' tiene que ir como AAAA-MM.")
    if estado not in ESTADOS_FACTURA:
        raise ErrorSistema(f"Estado de factura desconocido: '{estado}'.")
    numero = _siguiente(con, "factura", "factura", "FE-", 5, 92001)
    _alta(con, "factura", cuenta_id, numero, {"factura": numero, "cuenta_id": cuenta_id, "periodo": periodo,
                                              "valor": _entero(valor, "El valor de la factura", 1), "estado": estado})
    return numero


def _alta_contrato(con: sqlite3.Connection, cuenta_id: str, vigencia_hasta: str, valor_mensual: Any,
                   permite_upgrade: bool, permanencia_meses: Any) -> str:
    if _uno(con, "SELECT contrato FROM contrato WHERE cuenta_id = ?", cuenta_id):
        raise ErrorSistema(f"La cuenta {cuenta_id} ya tiene contrato.")
    try:
        datetime.strptime((vigencia_hasta or "").strip(), "%Y-%m-%d")
    except ValueError:
        raise ErrorSistema(f"La vigencia '{vigencia_hasta}' tiene que ir como AAAA-MM-DD.") from None
    numero = _siguiente(con, "contrato", "contrato", "CTO-", 4, 4001)
    _alta(con, "contrato", cuenta_id, numero, {
        "contrato": numero, "cuenta_id": cuenta_id, "vigencia_hasta": vigencia_hasta.strip(),
        "valor_mensual_pactado": _entero(valor_mensual, "El valor mensual", 1),
        "permite_upgrade_sin_adendum": int(bool(permite_upgrade)),
        "clausula_permanencia_meses": _entero(permanencia_meses or 0, "La permanencia")})
    return numero


def _exigir_cuenta(con: sqlite3.Connection, cuenta_id: str) -> None:
    if not _uno(con, "SELECT cuenta_id FROM cuenta WHERE cuenta_id = ?", cuenta_id):
        raise ErrorSistema(f"La cuenta {cuenta_id} no existe.")


def crear_cuenta(razon_social: str, *, segmento: str = "Corporativo", dominio: str = "",
                 estado: str = "activo", contactos: list[dict[str, Any]] | None = None,
                 servicios: list[dict[str, Any]] | None = None, contrato: dict[str, Any] | None = None,
                 facturas: list[dict[str, Any]] | None = None) -> str:
    """Da de alta un cliente completo en una sola transaccion: o entra todo o nada.

    `contactos`: [{correo, nombre, autorizado}]  (al menos uno: es por donde el
    copiloto reconoce al cliente). `servicios`: [{producto, sede, ancho_banda_mbps, nodo}].
    `contrato`: {vigencia_hasta, valor_mensual_pactado, permite_upgrade_sin_adendum,
    clausula_permanencia_meses}. `facturas`: [{periodo, valor, estado}].
    """
    razon_social = (razon_social or "").strip()
    if not razon_social:
        raise ErrorSistema("La razon social es obligatoria.")
    if not contactos:
        raise ErrorSistema("Agrega al menos un contacto: el copiloto reconoce al cliente por el correo.")
    with conexion() as con:
        cuenta_id = _siguiente(con, "cuenta", "cuenta_id", "B2B-", 6, 6001)
        dominio = (dominio or "").strip().lower().lstrip("@") or f"{cuenta_id.lower()}{_SIN_DOMINIO}"
        otra = _uno(con, "SELECT cuenta_id FROM cuenta WHERE dominio = ?", dominio)
        if otra:
            raise ErrorSistema(f"El dominio {dominio} ya es de la cuenta {otra['cuenta_id']}. "
                               "Dejalo vacio para identificar al cliente solo por sus correos.")
        _alta(con, "cuenta", cuenta_id, cuenta_id, {"cuenta_id": cuenta_id, "dominio": dominio,
                                                    "razon_social": razon_social, "segmento": segmento or "Corporativo",
                                                    "estado": estado or "activo"})
        for c in contactos:
            _alta_contacto(con, cuenta_id, c.get("correo", ""), c.get("nombre", ""), c.get("autorizado", True))
        for s in servicios or []:
            _alta_servicio(con, cuenta_id, s.get("producto", ""), s.get("sede", ""),
                           s.get("ancho_banda_mbps"), s.get("nodo", ""))
        if contrato:
            _alta_contrato(con, cuenta_id, contrato.get("vigencia_hasta", ""), contrato.get("valor_mensual_pactado"),
                           contrato.get("permite_upgrade_sin_adendum", True), contrato.get("clausula_permanencia_meses"))
        for f in facturas or []:
            _alta_factura(con, cuenta_id, f.get("periodo", ""), f.get("valor"), f.get("estado", "pendiente"))
        return cuenta_id


def agregar_contacto(cuenta_id: str, correo: str, nombre: str = "", autorizado: bool = True) -> str:
    """Sirve tambien para las cuentas sembradas: p. ej. poner tu propio correo en UFPSO."""
    with conexion() as con:
        _exigir_cuenta(con, cuenta_id)
        return _alta_contacto(con, cuenta_id, correo, nombre, autorizado)


def agregar_servicio(cuenta_id: str, sede: str, ancho_banda_mbps: Any, nodo: str,
                     producto: str = "Internet Dedicado") -> str:
    with conexion() as con:
        _exigir_cuenta(con, cuenta_id)
        return _alta_servicio(con, cuenta_id, producto, sede, ancho_banda_mbps, nodo)


def agregar_factura(cuenta_id: str, periodo: str, valor: Any, estado: str = "pendiente") -> str:
    with conexion() as con:
        _exigir_cuenta(con, cuenta_id)
        return _alta_factura(con, cuenta_id, periodo, valor, estado)


def agregar_contrato(cuenta_id: str, vigencia_hasta: str, valor_mensual_pactado: Any,
                     permite_upgrade_sin_adendum: bool = True, clausula_permanencia_meses: Any = 12) -> str:
    with conexion() as con:
        _exigir_cuenta(con, cuenta_id)
        return _alta_contrato(con, cuenta_id, vigencia_hasta, valor_mensual_pactado,
                              permite_upgrade_sin_adendum, clausula_permanencia_meses)


def _editar_alta(con: sqlite3.Connection, tabla: str, referencia: str, fila: dict[str, Any],
                 nueva_referencia: str | None = None) -> None:
    """Si la fila se dio de alta en la consola, `restaurar()` la vuelve a sembrar
    ya editada. Lo que viene de la semilla vuelve a sus valores iniciales."""
    con.execute("UPDATE alta SET referencia = ?, fila = ? WHERE tabla = ? AND referencia = ?",
                (nueva_referencia or referencia, json.dumps(fila, ensure_ascii=False), tabla, referencia))


def editar_cuenta(cuenta_id: str, *, razon_social: str, segmento: str, estado: str, dominio: str = "") -> None:
    """Los datos de la ficha. Un dominio vacio deja la cuenta sin dominio corporativo."""
    razon_social = (razon_social or "").strip()
    if not razon_social:
        raise ErrorSistema("La razon social es obligatoria.")
    if estado not in ESTADOS_CUENTA:
        raise ErrorSistema(f"Estado de cuenta desconocido: '{estado}'.")
    if not (segmento or "").strip():
        raise ErrorSistema("El segmento es obligatorio.")
    dominio = (dominio or "").strip().lower().lstrip("@")
    if dominio and ("." not in dominio or "@" in dominio or " " in dominio):
        raise ErrorSistema(f"'{dominio}' no es un dominio valido.")
    with conexion() as con:
        antes = _uno(con, "SELECT * FROM cuenta WHERE cuenta_id = ?", cuenta_id)
        if not antes:
            raise ErrorSistema(f"La cuenta {cuenta_id} no existe.")
        dominio = dominio or f"{cuenta_id.lower()}{_SIN_DOMINIO}"
        otra = _uno(con, "SELECT cuenta_id FROM cuenta WHERE dominio = ? AND cuenta_id != ?", dominio, cuenta_id)
        if otra:
            raise ErrorSistema(f"El dominio {dominio} ya es de la cuenta {otra['cuenta_id']}.")
        nuevos = {"razon_social": razon_social, "segmento": segmento.strip(), "estado": estado, "dominio": dominio}
        cambios = {k: v for k, v in nuevos.items() if antes[k] != v}
        if not cambios:
            return
        con.execute("UPDATE cuenta SET razon_social = ?, segmento = ?, estado = ?, dominio = ? WHERE cuenta_id = ?",
                    (razon_social, nuevos["segmento"], estado, dominio, cuenta_id))
        _editar_alta(con, "cuenta", cuenta_id, {**antes, **nuevos})
        _registrar(con, "CRM", "editar_cuenta", cuenta_id=cuenta_id, referencia=cuenta_id,
                   antes={k: antes[k] for k in cambios}, despues=cambios, detalle="Datos de la cuenta editados.")


def editar_contacto(cuenta_id: str, correo_actual: str, *, correo: str, nombre: str = "",
                    autorizado: bool = True) -> str:
    """Tambien el correo: es por donde el copiloto reconoce al cliente."""
    correo_actual = (correo_actual or "").strip().lower()
    correo = _correo_valido(correo)
    with conexion() as con:
        antes = _uno(con, "SELECT * FROM contacto WHERE correo = ? AND cuenta_id = ?", correo_actual, cuenta_id)
        if not antes:
            raise ErrorSistema(f"{correo_actual} no es contacto de la cuenta {cuenta_id}.")
        if correo != correo_actual:
            duenio = _uno(con, "SELECT cuenta_id FROM contacto WHERE correo = ?", correo)
            if duenio:
                raise ErrorSistema(f"El correo {correo} ya es contacto de la cuenta {duenio['cuenta_id']}.")
        nuevos = {"correo": correo, "nombre": (nombre or "").strip(), "autorizado": int(bool(autorizado))}
        cambios = {k: v for k, v in nuevos.items() if antes[k] != v}
        if not cambios:
            return correo
        con.execute("UPDATE contacto SET correo = ?, nombre = ?, autorizado = ? WHERE correo = ?",
                    (correo, nuevos["nombre"], nuevos["autorizado"], correo_actual))
        _editar_alta(con, "contacto", correo_actual, {**antes, **nuevos}, nueva_referencia=correo)
        _registrar(con, "CRM", "editar_contacto", cuenta_id=cuenta_id, referencia=correo,
                   antes={k: antes[k] for k in cambios}, despues=cambios, detalle="Contacto editado.")
        return correo


def eliminar_contacto(correo: str) -> None:
    """Solo los contactos dados de alta en la consola: la semilla no se toca."""
    correo = (correo or "").strip().lower()
    with conexion() as con:
        if not _uno(con, "SELECT id FROM alta WHERE tabla = 'contacto' AND referencia = ?", correo):
            raise ErrorSistema(f"{correo} viene de la semilla: no se puede eliminar desde la consola.")
        con.execute("DELETE FROM contacto WHERE correo = ?", (correo,))
        con.execute("DELETE FROM alta WHERE tabla = 'contacto' AND referencia = ?", (correo,))


def eliminar_cuenta(cuenta_id: str) -> None:
    """Borra un cliente creado en la consola con todo lo que cuelga de el."""
    with conexion() as con:
        if not _uno(con, "SELECT id FROM alta WHERE tabla = 'cuenta' AND referencia = ?", cuenta_id):
            raise ErrorSistema(f"La cuenta {cuenta_id} viene de la semilla: no se puede eliminar.")
        con.execute("DELETE FROM ticket_nota WHERE ticket IN (SELECT ticket FROM ticket WHERE cuenta_id = ?)",
                    (cuenta_id,))
        con.execute("DELETE FROM nota_credito WHERE factura IN (SELECT factura FROM factura WHERE cuenta_id = ?)",
                    (cuenta_id,))
        for tabla in ("ticket", "factura", "contrato", "servicio", "contacto", "operacion", "alta", "cuenta"):
            con.execute(f"DELETE FROM {tabla} WHERE cuenta_id = ?", (cuenta_id,))


def nodos() -> list[dict[str, Any]]:
    with conexion() as con:
        return [dict(r) for r in con.execute("SELECT * FROM nodo ORDER BY nodo")]


def dominio_visible(dominio: str) -> str:
    """El dominio tal como se muestra: vacio si la cuenta no tiene uno corporativo."""
    return "" if (dominio or "").endswith(_SIN_DOMINIO) else dominio


# --------------------------------------------------------------------------- #
# Lectura para la pestana "Sistemas"
# --------------------------------------------------------------------------- #
def cuentas() -> list[dict[str, Any]]:
    """Con `creada`: si se dio de alta en la consola (y por tanto se puede eliminar)."""
    with conexion() as con:
        creadas = {r[0] for r in con.execute("SELECT referencia FROM alta WHERE tabla = 'cuenta'")}
        return [{**dict(r), "creada": r["cuenta_id"] in creadas}
                for r in con.execute("SELECT * FROM cuenta ORDER BY razon_social")]


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
        altas = {r[0] for r in con.execute("SELECT referencia FROM alta WHERE cuenta_id = ?", (cuenta_id,))}
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
        # Lo dado de alta en la consola; `creada` si la cuenta entera lo fue.
        "altas": altas,
        "creada": cuenta_id in altas,
        "operaciones": operaciones(cuenta_id),
    }
