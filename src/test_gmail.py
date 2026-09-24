"""Pruebas de la normalizacion MIME de Gmail. Sin red y sin credenciales.

`a_correo` es funcion pura sobre el dict que devuelve la API, y ahi es donde
aparecen los bugs de verdad de una ingesta: el correo que solo trae HTML, el que
viene en latin-1, el que no manda Message-ID. El cliente HTTP no se prueba aqui
porque no hay nada nuestro que probar en el.
"""

import base64

from src.gmail_ingesta import a_correo, cuerpo_y_adjuntos, es_propio, html_a_texto


def _b64(texto: str, juego: str = "utf-8") -> str:
    return base64.urlsafe_b64encode(texto.encode(juego)).decode("ascii")


def _mensaje(payload: dict, **extra) -> dict:
    return {"id": "18f2a", "threadId": "THR-GM-1", "internalDate": "1790078400000", "payload": payload, **extra}


CABECERAS = [
    {"name": "Message-ID", "value": "<CAF-99@mail.acme.com.co>"},
    {"name": "From", "value": "laura.gomez@acme.com.co"},
    {"name": "To", "value": "care.b2b@operador.com, copia@operador.com"},
    {"name": "Subject", "value": "Ampliacion sede Bogota"},
    {"name": "Date", "value": "Mon, 22 Sep 2026 09:15:00 -0500"},
]


def test_multipart_prefiere_texto_plano_y_lista_adjuntos():
    msg = _mensaje({
        "mimeType": "multipart/mixed",
        "headers": CABECERAS,
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": _b64("Necesitamos 500 Mbps.")}},
                    {"mimeType": "text/html", "body": {"data": _b64("<p>Necesitamos <b>500</b> Mbps.</p>")}},
                ],
            },
            {"mimeType": "application/pdf", "filename": "factura.pdf", "body": {"attachmentId": "x"}},
        ],
    })
    correo = a_correo(msg)

    assert correo["cuerpo"] == "Necesitamos 500 Mbps."
    assert correo["adjuntos"] == ["factura.pdf"]
    assert correo["para"] == ["care.b2b@operador.com", "copia@operador.com"]
    assert correo["message_id"] == "<CAF-99@mail.acme.com.co>"
    assert correo["thread_id"] == "THR-GM-1"
    assert correo["recibido_en"].startswith("2026-09-22T09:15:00")


def test_correo_solo_html_se_degrada_a_texto():
    """Muchos clientes corporativos mandan solo HTML. Sin esto, el cuerpo que ve
    el modelo llega vacio y el caso sale sin solicitudes."""
    msg = _mensaje({
        "mimeType": "text/html",
        "headers": CABECERAS,
        "body": {"data": _b64(
            "<style>p{color:red}</style><div>Buenos dias,</div>"
            "<p>La factura lleg&oacute; por un valor distinto.</p><br>Gracias"
        )},
    })
    # El <br> suelto tras el </p> son dos saltos, y asi debe quedar: el espaciado
    # del correo original es parte de lo que el modelo lee.
    assert a_correo(msg)["cuerpo"] == "Buenos dias,\nLa factura llegó por un valor distinto.\n\nGracias"


def test_sin_message_id_se_cae_al_id_de_gmail():
    """Sin clave de idempotencia no hay ingesta segura; el id de Gmail sirve y
    tambien es estable."""
    msg = _mensaje({"mimeType": "text/plain", "headers": [], "body": {"data": _b64("hola")}})
    assert a_correo(msg)["message_id"] == "gmail:18f2a"


def test_charset_declarado_se_respeta():
    parte = {
        "mimeType": "text/plain",
        "headers": [{"name": "Content-Type", "value": 'text/plain; charset="iso-8859-1"'}],
        "body": {"data": _b64("ampliación de señal", "iso-8859-1")},
    }
    assert cuerpo_y_adjuntos(parte)[0] == "ampliación de señal"


def test_charset_desconocido_no_revienta():
    parte = {
        "mimeType": "text/plain",
        "headers": [{"name": "Content-Type", "value": "text/plain; charset=x-inventado"}],
        "body": {"data": _b64("hola")},
    }
    assert cuerpo_y_adjuntos(parte)[0] == "hola"


def test_sin_fecha_usa_internal_date():
    msg = _mensaje({"mimeType": "text/plain", "headers": [], "body": {"data": _b64("hola")}})
    assert a_correo(msg)["recibido_en"].startswith("2026-09-22T")


def test_html_a_texto_no_deja_etiquetas_ni_lineas_de_sobra():
    salida = html_a_texto("<div>uno</div><br><br><br><script>x()</script><p>dos</p>")
    assert "<" not in salida and "x()" not in salida
    assert salida == "uno\n\ndos"


def test_respuesta_enviada_desde_el_buzon_no_es_correo_entrante():
    """La respuesta del ejecutivo conserva el asunto 'Re: CARE-TEST ...' y entra
    en la query; si no se descarta, abre un caso con la solucion."""
    enviada = _mensaje({"mimeType": "text/plain", "headers": CABECERAS}, labelIds=["SENT"])
    borrador = _mensaje({"mimeType": "text/plain", "headers": CABECERAS}, labelIds=["DRAFT"])
    assert es_propio(enviada)
    assert es_propio(borrador)


def test_remitente_igual_al_buzon_es_propio_aunque_falte_la_etiqueta():
    msg = _mensaje({"mimeType": "text/plain", "headers": CABECERAS}, labelIds=["INBOX"])
    assert es_propio(msg, "Laura.Gomez@acme.com.co")
    assert not es_propio(msg, "care.b2b@operador.com")
    assert not es_propio(msg)


def test_correo_de_gmail_atraviesa_el_grafo():
    """La prueba que une las dos mitades: lo que produce `a_correo` es lo que
    `servicio.procesar_correo` sabe consumir."""
    from src import servicio

    msg = _mensaje({
        "mimeType": "text/plain",
        "headers": CABECERAS,
        "body": {"data": _b64(
            "Necesitamos ampliar el ancho de banda de la sede de Bogota a 500 Mbps.\n"
            "Aprovecho: la factura de septiembre llego por un valor distinto al pactado."
        )},
    })
    res = servicio.procesar_correo(a_correo(msg))

    assert res["estado"] == "pendiente"
    assert res["caso"]["cuenta"] == "B2B-004712"
    assert len(res["caso"]["solicitudes"]) == 2
