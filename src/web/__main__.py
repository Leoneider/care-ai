"""Arranca la UI:  python -m src.web  [--host H] [--port P] [--reload]"""

import argparse

import uvicorn


def main() -> None:
    ap = argparse.ArgumentParser(description="UI del ejecutivo Care")
    # 127.0.0.1 y no 0.0.0.0: la UI no tiene autenticacion todavia.
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reload", action="store_true", help="recarga al editar (desarrollo)")
    args = ap.parse_args()

    print(f"[care-ai] bandeja del ejecutivo en http://{args.host}:{args.port}")
    uvicorn.run("src.web.app:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
