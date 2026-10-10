"""Acceso al modelo, con un modo fake para poder correr el grafo sin API key.

`CARE_FAKE_LLM=1` devuelve respuestas canned. Sirve para:
- probar la topologia del grafo y el human-in-the-loop en CI,
- que un dev nuevo clone y corra `pytest` sin credenciales.
"""

import os
from typing import Any

MODELO = os.getenv("CARE_MODEL", "anthropic:claude-sonnet-4-6")


def get_structured_llm(schema: type, temperature: float = 0.0) -> Any:
    if os.getenv("CARE_FAKE_LLM") == "1":
        from .fakes import FakeStructuredLLM

        return FakeStructuredLLM(schema)

    from langchain.chat_models import init_chat_model

    return init_chat_model(MODELO, temperature=temperature).with_structured_output(schema)
