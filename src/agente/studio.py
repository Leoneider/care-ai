"""Punto de entrada para `langgraph dev` / LangGraph Studio.

Exporta la topologia SIN compilar a proposito. El servidor de LangGraph trae su
propia capa de persistencia y aborta el arranque si el grafo ya viene con un
checkpointer propio; al entregarle el builder, el compila con el suyo.

Consecuencia practica: los casos que abras en Studio viven en la persistencia del
servidor, no en `data/care.db`. La UI del ejecutivo (`src/web`) y el poller de
Gmail siguen usando `construir_grafo()` con SqliteSaver y no comparten bandeja
con Studio.
"""

from .graph import topologia

grafo = topologia()
