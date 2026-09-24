"""APARCADO: escrito pero NO conectado al grafo.

`aplicar_decision` sigue escribiendo feedback.jsonl a mano. Para conectarlo:
    1. nodes.py:  from . import backends, feedback, validaciones
    2. nodes.py:  reemplazar el bloque del open() por
                  feedback.guardar(caso, decision, state["correo"].get("message_id", ""))
    3. .env:      CARE_FEEDBACK_DSN=sqlite:///data/feedback.db
    4. conftest.py con un fixture que apunte el DSN a tmp_path, o pytest
       volvera a escribir en el dataset real.

Las metricas del README, como consultas SQL.

    python -m src.metricas

Este modulo es la razon de mover el feedback a una BD. Con el JSONL habia que
leer el archivo entero a RAM y recorrerlo en Python para cada cifra; aqui cada
una es una consulta sobre columnas indexadas y el costo no depende de cuanto
haya crecido el dataset.
"""

from . import feedback


def main() -> None:
    with feedback.conexion() as con:
        total = con.execute("SELECT COUNT(*) FROM revision").fetchone()[0]
        if not total:
            print(f"BD vacia ({feedback.dsn()}). Corre el demo o el migrador primero.")
            return

        print(f"=== {total} revisiones | {feedback.dsn()} ===\n")

        print("Decision del ejecutivo")
        print("  (aprobar sin editar es la metrica que dice si el copiloto ahorra trabajo)")
        for dec, n in con.execute(
            "SELECT decision, COUNT(*) FROM revision GROUP BY decision ORDER BY COUNT(*) DESC"
        ):
            print(f"    {dec or '(vacia)':12} {n:5}  {n/total:6.1%}")

        print("\nPor tipo de solicitud")
        for tipo, n, conf, aten, falt in con.execute(
            "SELECT tipo, COUNT(*), AVG(confianza), AVG(requiere_atencion), AVG(n_faltantes) "
            "FROM solicitud_revisada GROUP BY tipo ORDER BY COUNT(*) DESC"
        ):
            print(f"    {tipo:30} n={n:4}  confianza {conf:.2f}  "
                  f"atencion {aten:5.1%}  faltantes {falt:.1f}")

        print("\nCalibracion de la confianza")
        print("  (si las aprobadas no tienen confianza mas alta, el umbral de 0.75 esta mal puesto)")
        for dec, n, conf in con.execute(
            "SELECT r.decision, COUNT(*), AVG(s.confianza) "
            "FROM revision r JOIN solicitud_revisada s ON s.revision_id = r.id "
            "GROUP BY r.decision ORDER BY AVG(s.confianza) DESC"
        ):
            print(f"    {dec or '(vacia)':12} n={n:4}  confianza media {conf:.2f}")


if __name__ == "__main__":
    main()
