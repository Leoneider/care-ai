"""Arranca el poller de Gmail:  python -m src.poller  [--loop SEG] [--dry-run] ...

Es el proceso que barre el buzon y corre el tramo 1 del grafo. El cliente de Gmail
vive en `src/gmail_ingesta.py` y no aqui porque la bandeja tambien lo usa (borrador
de respuesta y etiqueta al cerrar el caso). Acepta los mismos argumentos que
`python -m src.gmail_ingesta`.
"""

from ..gmail_ingesta import main

if __name__ == "__main__":
    main()
