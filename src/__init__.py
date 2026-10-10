"""Carga de configuracion del proyecto.

Este modulo corre antes que cualquier submodulo de `src`, y ese es justo el
momento en que hay que leer el .env: `llm.py` resuelve CARE_MODEL a nivel de
modulo y LangSmith lee sus variables al abrir la primera traza. Cargarlo mas
tarde (dentro de `demo.main`, por ejemplo) llegaria despues de los imports.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# override=False: lo que ya este en el entorno real manda sobre el .env, para
# que `CARE_FAKE_LLM=1 python -m src.poller.gmail_ingesta` siga funcionando.
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

os.environ.setdefault("LANGSMITH_PROJECT", "care-ai")

# Sin credencial, LangSmith no traza y tampoco protesta: el grafo corre igual y
# uno se queda esperando unas trazas que nunca van a aparecer. Mejor decirlo.
if os.getenv("LANGSMITH_TRACING", "").lower() == "true" and not os.getenv("LANGSMITH_API_KEY"):
    os.environ["LANGSMITH_TRACING"] = "false"
    print(
        "[care-ai] LANGSMITH_TRACING=true pero LANGSMITH_API_KEY esta vacia: tracing desactivado.\n"
        "          Genera una key en https://smith.langchain.com > Settings > API Keys y pegala en .env"
    )
