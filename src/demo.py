"""Demo end-to-end: correo -> caso -> pausa -> decision del ejecutivo -> ejecucion.

    CARE_FAKE_LLM=1 python -m src.demo                  # sin API key
    ANTHROPIC_API_KEY=... python -m src.demo
    python -m src.demo --solo-ingesta                   # deja el caso para la UI
    python -m src.demo --reiniciar                      # olvida el correo de ejemplo

Con `--solo-ingesta` el demo se detiene en el `interrupt` y no decide nada: el
caso queda en la bandeja para que lo resuelvas desde `python -m src.web`. Es la
forma de ver el flujo real, en el que el tramo 2 lo dispara un humano y no un
`decision = {...}` escrito dos lineas mas abajo.

Todo pasa por `servicio.py`, igual que la ingesta de Gmail y la UI: el demo no
tiene un camino privilegiado al grafo.

Con LANGSMITH_TRACING=true y LANGSMITH_API_KEY en el .env, cada corrida imprime
al final el enlace a su traza: un span por nodo, con entradas y salidas.
"""

import argparse
import json

from . import db, servicio
from .tracing import anunciar, traza

CORREO = {
    "message_id": "<CAF-2026-0921-77120@ufpso.edu.co>",
    "thread_id": "THR-77120",
    "remitente": "ltrigosg@ufpso.edu.co",
    "para": ["care.b2b@operador.com"],
    "asunto": "Ampliacion sede Bogota + revision factura septiembre",
    "cuerpo": (
        "Buenos dias,\n\n"
        "Necesitamos ampliar el ancho de banda de la sede de Bogota a 500 Mbps, "
        "el trafico nos viene creciendo y ya estamos al tope.\n\n"
        "Aprovecho tambien: la factura de septiembre llego por un valor distinto "
        "al pactado en el contrato. Les agradezco la revision.\n\n"
        "Gracias,\nLeoneider Trigos\n"
        "--\nLeoneider Trigos | UFPSO\n"
    ),
    "adjuntos": [],
}


def imprimir_caso(caso: dict) -> None:
    print("=" * 78)
    print(f"CASO {caso['caso_id']}  |  {caso['razon_social']} ({caso['cuenta']})")
    print(f"Prioridad {caso['prioridad']}  |  confianza global {caso['confianza_global']}  "
          f"|  requiere atencion: {caso['requiere_atencion']}")
    print("=" * 78)
    for s in caso["solicitudes"]:
        print(f"\n[{s['id']}] {s['tipo']}  (confianza {s['confianza']})")
        print(f"  Resumen : {s['resumen']}")
        print(f"  Fuente  : \"{s['texto_fuente']}\"")
        if s["datos"]:
            print(f"  Datos   : {json.dumps(s['datos'], ensure_ascii=False)}")
        if s["faltantes"]:
            print(f"  Faltan  : {', '.join(s['faltantes'])}")
        print("  Validaciones:")
        for v in s["validaciones"]:
            marca = {"ok": "OK  ", "alerta": "WARN", "falla": "FAIL"}[v["resultado"]]
            print(f"    [{marca}] {v['check']}: {v['detalle']}")
        print("  Acciones propuestas:")
        for i, a in enumerate(s["acciones"], 1):
            print(f"    {i}. ({a['sistema']}) {a['codigo']} - {a['descripcion']}")
        if s["requiere_atencion"]:
            print(f"  !! Atencion: {s['motivo_atencion']}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Demo end-to-end del grafo Care")
    ap.add_argument("--solo-ingesta", action="store_true",
                    help="para en el interrupt y deja el caso en la bandeja para la UI")
    ap.add_argument("--reiniciar", action="store_true",
                    help="olvida el correo de ejemplo para poder repetir el demo")
    args = ap.parse_args()

    if args.reiniciar:
        with db.conexion() as con:
            con.execute("DELETE FROM caso WHERE message_id = ?", (CORREO["message_id"],))
        print(f"[demo] {CORREO['message_id']} olvidado. La proxima corrida vuelve a crear caso.")
        return

    # Tramo 1: ingesta -> ... -> interrupt. Es una traza completa por si sola.
    with traza("tramo-1-hasta-revision") as cb:
        res = servicio.procesar_correo(CORREO)
    anunciar(cb, "tramo 1: ingesta -> revision humana")

    if res["estado"] != "pendiente":
        print(f"\nEl grafo no dejo caso para revisar (estado: {res['estado']}).")
        if res["estado"] == "duplicado":
            print("Este correo ya se proceso. Para repetir el demo:  python -m src.demo --reiniciar")
        return

    imprimir_caso(res["caso"])
    thread_id = res["thread_id"]

    print("\n--- grafo PAUSADO esperando al ejecutivo Care ---")
    print(f"thread: {thread_id}")

    if args.solo_ingesta:
        print("\nEl caso quedo en la bandeja. Levanta la UI para resolverlo:")
        print("    python -m src.web        ->  http://127.0.0.1:8000")
        return

    # Aqui, en la vida real, responde la UI del ejecutivo (dias despues si quiere).
    # El tramo 2 es una traza aparte y no la continuacion de la primera, porque en
    # produccion ni siquiera corre en el mismo proceso.
    with traza("tramo-2-post-decision") as cb:
        final = servicio.resolver(thread_id, "aprobar", "Acciones correctas, se ejecutan.")
    anunciar(cb, "tramo 2: decision -> ejecucion")

    print("\nResultado:", json.dumps(final["resultado"], ensure_ascii=False, indent=2))
    print("\nTraza:")
    for t in final["traza"]:
        print("  -", t)


if __name__ == "__main__":
    main()
