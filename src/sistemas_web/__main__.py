"""Arranca la consola del mini sistema legado:  python -m src.sistemas_web  [--host H] [--port P] [--reload]"""

import argparse

import uvicorn


def main() -> None:
    ap = argparse.ArgumentParser(description="Consola del mini sistema legado (CRM, facturacion, red, tickets)")
    # 127.0.0.1 y no 0.0.0.0: como la bandeja, no tiene autenticacion.
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8002)
    ap.add_argument("--reload", action="store_true", help="recarga al editar (desarrollo)")
    args = ap.parse_args()

    print(f"[sistemas] consola del sistema legado en http://{args.host}:{args.port}")
    uvicorn.run("src.sistemas_web.app:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
