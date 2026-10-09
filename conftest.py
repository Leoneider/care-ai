"""Aislamiento de las pruebas.

Dos cosas que antes no estaban y el README ya senalaba como deuda:

- **Ni la BD ni el dataset de pruebas son los de trabajo.** `CARE_DB` y
  `CARE_FEEDBACK_JSONL` apuntan a temporales, asi que `pytest` no ensucia la
  bandeja ni el `feedback.jsonl` real. Antes, cada corrida metia filas sinteticas
  en el mismo archivo que las revisiones de verdad, y con la UI y el servicio
  cubiertos por pruebas eso pasaba de 6 filas por corrida a varias decenas.
- **El checkpointer va en memoria.** Las pruebas son de topologia y de la capa de
  servicio: no tienen por que dejar archivos ni depender de lo que quedo de la
  corrida anterior. `CARE_CHECKPOINT=memory` lo garantiza.

Se fija a nivel de modulo, no en un fixture, porque `pytest` importa los modulos
de prueba antes de resolver fixtures y `src.agente.graph` decide su checkpointer al
construir el grafo.
"""

import os
import shutil
import tempfile

_TMP = tempfile.mkdtemp(prefix="care-ai-tests-")

os.environ["CARE_FAKE_LLM"] = "1"
os.environ["CARE_CHECKPOINT"] = "memory"
os.environ["CARE_DB"] = os.path.join(_TMP, "care_test.db")
os.environ["CARE_FEEDBACK_JSONL"] = os.path.join(_TMP, "feedback_test.jsonl")
os.environ["CARE_SISTEMAS_DB"] = os.path.join(_TMP, "sistemas_test.db")
# Red de seguridad: hay un token.json real en la raiz. Si una prueba olvida
# sustituir el cliente de Gmail, que falle por falta de token y no escriba en
# el buzon de verdad.
os.environ["GMAIL_TOKEN"] = os.path.join(_TMP, "sin-token.json")
# El .env de trabajo puede tener los borradores encendidos; las pruebas que los
# necesitan lo activan con monkeypatch.
os.environ["CARE_BORRADOR_GMAIL"] = "0"
# "false" y no pop(): src/__init__ carga el .env con override=False, y una
# variable ausente la rellena con el LANGSMITH_TRACING=true del .env.
os.environ["LANGSMITH_TRACING"] = "false"  # las pruebas no mandan trazas

import pytest  # noqa: E402

from src.agente import db, servicio  # noqa: E402
from src.sistemas_web import sistemas  # noqa: E402


@pytest.fixture(autouse=True)
def bandeja_limpia():
    """Cada prueba arranca sin casos y con un grafo nuevo.

    `servicio.reiniciar()` importa: el grafo se cachea por proceso y con el saver
    en memoria ese cache ES el estado, asi que sin esto una prueba veria los
    threads de la anterior.
    """
    db.limpiar()
    # La Fase 2 escribe en el mini sistema: cada prueba parte de la semilla, sin
    # los clientes que otra prueba haya dado de alta.
    sistemas.restaurar(conservar_altas=False)
    servicio.reiniciar()
    yield
    db.limpiar()
    servicio.reiniciar()


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TMP, ignore_errors=True)
