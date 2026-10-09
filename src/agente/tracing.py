"""Traza de la corrida en LangSmith.

Con LANGSMITH_TRACING=true, LangGraph ya reporta un span por nodo sin que haya
que tocar nada. Lo que no da gratis es agrupar la corrida bajo un nombre propio
y devolver el enlace, que es lo unico que agrega este modulo.

Cada `invoke` del grafo es una traza distinta: la primera va de la ingesta al
interrupt, la segunda arranca cuando el ejecutivo decide. En produccion esas
dos pueden estar separadas por dias y por procesos distintos.
"""

import os
from contextlib import contextmanager
from typing import Any, Iterator


def tracing_activo() -> bool:
    return os.getenv("LANGSMITH_TRACING", "").lower() == "true"


@contextmanager
def traza(nombre: str, tags: list[str] | None = None) -> Iterator[Any]:
    """Abre un run raiz en LangSmith. Si el tracing esta apagado, no hace nada."""
    if not tracing_activo():
        yield None
        return

    from langchain_core.tracers.context import tracing_v2_enabled

    with tracing_v2_enabled(
        project_name=os.getenv("LANGSMITH_PROJECT", "care-ai"),
        tags=[nombre, *(tags or [])],
    ) as cb:
        yield cb


def anunciar(cb: Any, etiqueta: str) -> None:
    """Imprime el enlace a la traza, o como encenderla si esta apagada."""
    if cb is None:
        print(
            f"\n[traza: {etiqueta}] tracing apagado. "
            "Pon LANGSMITH_TRACING=true y tu LANGSMITH_API_KEY en .env para verla en LangSmith."
        )
        return
    try:
        print(f"\n[traza: {etiqueta}] {cb.get_run_url()}")
    except Exception as exc:  # el run puede no haberse enviado todavia
        print(f"\n[traza: {etiqueta}] no se pudo resolver la URL ({exc}). "
              f"Revisa el proyecto '{os.getenv('LANGSMITH_PROJECT')}' en https://smith.langchain.com")
