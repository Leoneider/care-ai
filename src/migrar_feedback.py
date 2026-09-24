"""APARCADO: escrito pero NO conectado al grafo.

`aplicar_decision` sigue escribiendo feedback.jsonl a mano. Para conectarlo:
    1. nodes.py:  from . import backends, feedback, validaciones
    2. nodes.py:  reemplazar el bloque del open() por
                  feedback.guardar(caso, decision, state["correo"].get("message_id", ""))
    3. .env:      CARE_FEEDBACK_DSN=sqlite:///data/feedback.db
    4. conftest.py con un fixture que apunte el DSN a tmp_path, o pytest
       volvera a escribir en el dataset real.

Importa un feedback.jsonl existente a la BD.

    python -m src.migrar_feedback [ruta.jsonl]

Idempotente por (caso_id, creado_en) no: el JSONL no guarda fecha, asi que
correrlo dos veces duplica. Se corre una vez y se archiva el .jsonl.
"""

import json
import sys
from pathlib import Path

from . import feedback


def migrar(origen: Path) -> tuple[int, int]:
    if not origen.exists():
        print(f"No existe {origen}")
        return 0, 0

    leidas = insertadas = 0
    for linea in origen.read_text(encoding="utf-8").splitlines():
        if not linea.strip():
            continue
        leidas += 1
        reg = json.loads(linea)
        # El JSONL guardaba las solicitudes bajo "propuesto"; la BD las quiere
        # dentro de un caso, que es la forma en que las produce el grafo.
        caso = {"caso_id": reg.get("caso_id", ""), "solicitudes": reg.get("propuesto", [])}
        decision = {
            "decision": reg.get("decision", ""),
            "comentario": reg.get("comentario", ""),
            "acciones_finales": reg.get("final"),
        }
        feedback.guardar(caso, decision, message_id="")
        insertadas += 1
    return leidas, insertadas


def main() -> None:
    origen = Path(sys.argv[1]) if len(sys.argv) > 1 else feedback.RAIZ / "feedback.jsonl"
    leidas, insertadas = migrar(origen)
    rev, sol = feedback.contar()
    print(f"Leidas {leidas} lineas de {origen.name}, insertadas {insertadas}.")
    print(f"BD ({feedback.dsn()}): {rev} revisiones, {sol} solicitudes.")
    if insertadas:
        print(f"\nRevisa el resultado y archiva el original:  mv {origen.name} {origen.name}.migrado")


if __name__ == "__main__":
    main()
