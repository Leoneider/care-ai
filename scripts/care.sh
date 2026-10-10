#!/usr/bin/env bash
#
# Arranque local de Care AI en macOS / Linux. Equivalente de scripts/care.ps1.
#
#     ./care up          bandeja + consola de sistemas + poller de Gmail
#     ./care ui          solo la bandeja
#     ./care sistemas    solo la consola del sistema legado (otra app)
#     ./care ventana     la consola en su propia ventana, como programa de escritorio
#     ./care poller      solo la ingesta
#     ./care studio      el grafo en LangGraph Studio (langgraph dev)
#     ./care gmail       conecta con Google: guia, autoriza y comprueba
#     ./care estado      que corre y que hay en la bandeja
#     ./care stop        para todo
#     ./care limpiar     vacia la bandeja
#     ./care reiniciar   desde cero: bandeja vacia y sistemas en la semilla
#     ./care restaurar   devuelve el mini sistema (CRM, facturacion, red) a la semilla
#
# Mismas razones que el .ps1: usa siempre el python del venv (y lo crea si falta)
# y libera el puerto si quedo un servidor viejo colgado. Todo escucha en 127.0.0.1.
#
# Variables opcionales:  PUERTO=8000  PUERTO_SISTEMAS=8002  INTERVALO=60  PUERTO_STUDIO=2024

set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$RAIZ/.venv"
PY="$VENV/bin/python"
DIR_LOGS="$RAIZ/data/logs"
PIDS="$RAIZ/data/.pids"

PUERTO="${PUERTO:-8000}"
PUERTO_SISTEMAS="${PUERTO_SISTEMAS:-8002}"
INTERVALO="${INTERVALO:-60}"
PUERTO_STUDIO="${PUERTO_STUDIO:-2024}"

# --------------------------------------------------------------------------- #
# Salida con color
# --------------------------------------------------------------------------- #
if [ -t 1 ]; then
    C_RESET=$'\033[0m'; C_GRIS=$'\033[90m'; C_VERDE=$'\033[32m'
    C_AMARILLO=$'\033[33m'; C_ROJO=$'\033[31m'; C_CIAN=$'\033[36m'
else
    C_RESET=""; C_GRIS=""; C_VERDE=""; C_AMARILLO=""; C_ROJO=""; C_CIAN=""
fi
gris()     { printf '%s%s%s\n' "$C_GRIS" "$*" "$C_RESET"; }
verde()    { printf '%s%s%s\n' "$C_VERDE" "$*" "$C_RESET"; }
amarillo() { printf '%s%s%s\n' "$C_AMARILLO" "$*" "$C_RESET"; }
rojo()     { printf '%s%s%s\n' "$C_ROJO" "$*" "$C_RESET"; }
cian()     { printf '%s%s%s\n' "$C_CIAN" "$*" "$C_RESET"; }

# --------------------------------------------------------------------------- #
# Entorno
# --------------------------------------------------------------------------- #
buscar_python() {
    # El proyecto va en 3.12; se acepta cualquier 3.11+ si no hay otro.
    local candidato
    for candidato in python3.12 python3.13 python3.11 python3; do
        if command -v "$candidato" >/dev/null 2>&1; then
            if "$candidato" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
                command -v "$candidato"; return 0
            fi
        fi
    done
    return 1
}

confirmar_entorno() {
    if [ ! -x "$PY" ]; then
        amarillo "No hay venv en .venv. Creandolo..."
        local base
        if ! base="$(buscar_python)"; then
            rojo "No encontre Python 3.11+. Instala uno, por ejemplo:  brew install python@3.12"
            exit 1
        fi
        gris "  usando $base ($("$base" --version 2>&1))"
        "$base" -m venv "$VENV"
        "$PY" -m pip install -q --upgrade pip
        "$PY" -m pip install -q -r "$RAIZ/requirements.txt"
        verde "Dependencias instaladas."
    fi

    # Una comprobacion barata que atrapa el venv a medio instalar.
    if ! "$PY" -c "import fastapi, uvicorn, langgraph, googleapiclient" 2>/dev/null; then
        amarillo "Faltan dependencias. Instalando..."
        "$PY" -m pip install -q -r "$RAIZ/requirements.txt"
    fi

    mkdir -p "$DIR_LOGS"
}

pids_en_puerto() {
    lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | sort -u || true
}

liberar_puerto() {
    local puerto="$1" ids
    ids="$(pids_en_puerto "$puerto")"
    if [ -n "$ids" ]; then
        amarillo "El puerto $puerto estaba ocupado (PID $(echo "$ids" | tr '\n' ' ')). Liberandolo."
        echo "$ids" | xargs kill -9 2>/dev/null || true
        sleep 0.7
    fi
}

anotar_pid() { echo "$1=$2" >> "$PIDS"; }

esperar_http() {
    # Esperar a que conteste antes de decir que esta lista: si murio al arrancar,
    # es mejor enterarse aqui que al abrir el navegador.
    local url="$1" i
    for i in $(seq 1 25); do
        sleep 0.4
        if curl -fsS --max-time 2 -o /dev/null "$url" 2>/dev/null; then return 0; fi
    done
    return 1
}

# --------------------------------------------------------------------------- #
# Procesos
# --------------------------------------------------------------------------- #
iniciar_ui() {
    liberar_puerto "$PUERTO"
    local log="$DIR_LOGS/ui.log"
    ( cd "$RAIZ" && nohup "$PY" -m src.web --port "$PUERTO" >"$log" 2>"$log.err" & echo $! > "$DIR_LOGS/.ultimo" )
    anotar_pid ui "$(cat "$DIR_LOGS/.ultimo")"
    local url="http://127.0.0.1:$PUERTO/"
    if esperar_http "$url"; then verde "  bandeja  -> $url"; return 0; fi
    rojo "  La UI no respondio. Revisa $log.err"; return 1
}

iniciar_sistemas() {
    # La consola del mini sistema legado es otra app, en otro proceso y puerto.
    liberar_puerto "$PUERTO_SISTEMAS"
    local log="$DIR_LOGS/sistemas.log"
    ( cd "$RAIZ" && nohup "$PY" -m src.sistemas_web --port "$PUERTO_SISTEMAS" >"$log" 2>"$log.err" & echo $! > "$DIR_LOGS/.ultimo" )
    anotar_pid sistemas "$(cat "$DIR_LOGS/.ultimo")"
    local url="http://127.0.0.1:$PUERTO_SISTEMAS/"
    if esperar_http "$url"; then verde "  sistemas -> $url"; return 0; fi
    rojo "  La consola de sistemas no respondio. Revisa $log.err"; return 1
}

iniciar_poller() {
    if [ ! -f "$RAIZ/token.json" ]; then
        amarillo "  poller   -> omitido: falta token.json"
        gris     "              autoriza una vez:  ./care gmail"
        return 1
    fi
    local log="$DIR_LOGS/poller.log"
    # -u (sin buffer): con la salida a archivo, Python acumula stdout en bloques
    # de 8KB y el log se ve vacio aunque el poller este trabajando.
    ( cd "$RAIZ" && nohup "$PY" -u -m src.poller.gmail_ingesta --loop "$INTERVALO" >"$log" 2>"$log.err" & echo $! > "$DIR_LOGS/.ultimo" )
    anotar_pid poller "$(cat "$DIR_LOGS/.ultimo")"
    verde "  poller   -> barriendo Gmail cada $INTERVALO s (log: data/logs/poller.log)"
    return 0
}

detener_todo() {
    local parados=0 linea pid ids
    if [ -f "$PIDS" ]; then
        while IFS= read -r linea; do
            pid="${linea#*=}"
            if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
                # El python3 del instalador de python.org es un stub que lanza el
                # interprete real como hijo: hay que matar primero a los hijos.
                pkill -9 -P "$pid" 2>/dev/null || true
                kill -9 "$pid" 2>/dev/null || true
                parados=$((parados + 1))
            fi
        done < "$PIDS"
        rm -f "$PIDS"
        sleep 0.3   # que el kernel suelte los puertos antes de volver a mirarlos
    fi
    # Por si quedo algo de una sesion anterior cuyo .pids se perdio.
    for p in "$PUERTO" "$PUERTO_SISTEMAS"; do
        ids="$(pids_en_puerto "$p")"
        if [ -n "$ids" ]; then
            echo "$ids" | xargs kill -9 2>/dev/null || true
            parados=$((parados + $(echo "$ids" | wc -l | tr -d ' ')))
        fi
    done
    # El poller no escucha en ningun puerto: se busca por linea de comando.
    ids="$(pgrep -f 'gmail_ingesta.*--loop' || true)"
    if [ -n "$ids" ]; then
        echo "$ids" | xargs kill -9 2>/dev/null || true
        parados=$((parados + $(echo "$ids" | wc -l | tr -d ' ')))
    fi
    verde "Detenidos: $parados proceso(s)."
}

mostrar_estado() {
    echo; cian "Procesos"
    if [ -n "$(pids_en_puerto "$PUERTO")" ]; then verde "  bandeja  arriba  http://127.0.0.1:$PUERTO/"; else gris "  bandeja  parada"; fi
    if [ -n "$(pids_en_puerto "$PUERTO_SISTEMAS")" ]; then verde "  sistemas arriba  http://127.0.0.1:$PUERTO_SISTEMAS/"; else gris "  sistemas parada"; fi
    local poller; poller="$(pgrep -f 'gmail_ingesta.*--loop' || true)"
    if [ -n "$poller" ]; then verde "  poller   arriba  (PID $(echo "$poller" | tr '\n' ' '))"; else gris "  poller   parado"; fi

    echo; cian "Bandeja"
    ( cd "$RAIZ" && "$PY" - <<'PYEOF'
from src.agente import servicio, db
r = servicio.resumen()
print(f"  {r['pendientes']} pendiente(s), {r['en_verificacion']} en verificacion, {r['resueltos']} resuelto(s)")
for c in servicio.listar(db.PENDIENTE)[:10] + servicio.listar(db.EN_VERIFICACION)[:10]:
    marca = 'verif' if c['estado'] == db.EN_VERIFICACION else f"P{c['prioridad']}   "
    print(f"    {marca}  {c['caso_id']:22} {c['razon_social'][:24]:24} {c['asunto'][:40]}")
PYEOF
    )
    echo
}

abrir_url() {
    if command -v open >/dev/null 2>&1; then open "$1"; else xdg-open "$1" >/dev/null 2>&1 || true; fi
}

ayuda() {
    cat <<EOT

Care AI - arranque local

  ./care gmail       conecta con Google: guia, autoriza y comprueba
  ./care up          bandeja + sistemas + poller de Gmail
  ./care ui          solo la bandeja       -> http://127.0.0.1:$PUERTO
  ./care sistemas    solo la consola del sistema legado -> http://127.0.0.1:$PUERTO_SISTEMAS
  ./care ventana     la consola en su propia ventana (modo aplicacion de escritorio)
  ./care poller      solo la ingesta de Gmail
  ./care studio      el grafo en LangGraph Studio -> http://127.0.0.1:$PUERTO_STUDIO
  ./care estado      que corre y que hay en la bandeja
  ./care stop        para todo
  ./care limpiar     vacia la bandeja
  ./care reiniciar   desde cero: bandeja vacia y sistemas en la semilla
  ./care restaurar   devuelve el mini sistema (CRM, facturacion, red) a la semilla

Opciones (variables de entorno):  PUERTO=8000  PUERTO_SISTEMAS=8002  INTERVALO=60  PUERTO_STUDIO=2024
  ej.  PUERTO=8080 ./care ui

Primera vez:  ./care gmail   ->   ./care up

EOT
}

# --------------------------------------------------------------------------- #
# Despacho
# --------------------------------------------------------------------------- #
COMANDO="${1:-ayuda}"
case "$COMANDO" in
    up)
        confirmar_entorno
        echo; cian "Care AI"
        iniciar_ui || true
        iniciar_sistemas || true
        iniciar_poller || true
        echo; gris "  ./care estado   ver que hay"; gris "  ./care stop     parar todo"; echo
        ;;
    ui)
        confirmar_entorno; echo; cian "Care AI"; iniciar_ui || true; echo ;;
    sistemas)
        confirmar_entorno; echo; cian "Sistemas legados"; iniciar_sistemas || true; echo ;;
    ventana)
        # La consola en una ventana propia (Chrome/Edge en modo app: sin pestanas ni
        # barra de direcciones), para que en la demo se vea como el programa de
        # escritorio de otro equipo. Si no esta corriendo, se arranca primero.
        confirmar_entorno
        url="http://127.0.0.1:$PUERTO_SISTEMAS/"
        if ! curl -fsS --max-time 2 -o /dev/null "$url" 2>/dev/null; then
            echo; cian "Sistemas legados"
            iniciar_sistemas || exit 1
        fi
        navegador=""
        for app in "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
                   "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge" \
                   "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"; do
            if [ -x "$app" ]; then navegador="$app"; break; fi
        done
        if [ -n "$navegador" ]; then
            "$navegador" --app="$url" --window-size=1320,860 >/dev/null 2>&1 &
            verde "  ventana -> $url"
        else
            abrir_url "$url"
            amarillo "  No encontre Chrome ni Edge: se abrio en el navegador por defecto."
        fi
        ;;
    poller)
        confirmar_entorno; echo; cian "Care AI"; iniciar_poller || true; echo ;;
    gmail)
        confirmar_entorno
        # Google lo descarga como client_secret_<id>.apps.googleusercontent.com.json;
        # `ruta_secreto()` en Python acepta ambos nombres, y aqui se busca igual.
        secreto="$RAIZ/credentials.json"
        if [ ! -f "$secreto" ]; then
            hallado="$(ls "$RAIZ"/client_secret*.json 2>/dev/null | head -1 || true)"
            [ -n "$hallado" ] && secreto="$hallado"
        fi
        token="$RAIZ/token.json"
        if [ ! -f "$secreto" ]; then
            amarillo "
Falta credentials.json, y ese paso es tuyo: hay que crearlo con tu cuenta de
Google. En https://console.cloud.google.com

  1. Crea un proyecto (o elige uno).
  2. Habilita la \"Gmail API\" desde la biblioteca de APIs.
  3. Configura la pantalla de consentimiento OAuth, tipo Externo, y AGREGATE
     como usuario de prueba. Si te saltas eso, el login falla al final.
  4. Crea credenciales -> ID de cliente de OAuth -> App de escritorio.
  5. Descarga el JSON y guardalo aqui como:

     $secreto

Cuando lo tengas, vuelve a correr:  ./care gmail
"
            exit 0
        fi
        if [ ! -f "$token" ]; then
            echo; cian "Autorizando. Se abrira el navegador."
            gris "Si dice 'Google no ha verificado esta aplicacion', es tu propia app:"
            gris "Configuracion avanzada -> Ir a (no seguro)."; echo
            ( cd "$RAIZ" && "$PY" -m src.poller.gmail_ingesta --autorizar ) || { rojo "La autorizacion fallo."; exit 1; }
        fi
        ( cd "$RAIZ" && "$PY" -m src.poller.gmail_ingesta --diagnostico )
        ;;
    studio)
        confirmar_entorno
        cli="$VENV/bin/langgraph"
        if ! "$PY" -c "import langgraph_api" 2>/dev/null || [ ! -x "$cli" ]; then
            amarillo "Instalando langgraph-cli[inmem] en el venv..."
            "$PY" -m pip install -q "langgraph-cli[inmem]"
        fi
        liberar_puerto "$PUERTO_STUDIO"
        echo; cian "Care AI - LangGraph Studio"
        verde "  api     -> http://127.0.0.1:$PUERTO_STUDIO"
        verde "  studio  -> https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:$PUERTO_STUDIO"
        gris  "  Ctrl+C para parar."; echo
        # --allow-blocking: los nodos leen SQLite y el mini sistema de forma
        # sincrona; sin la bandera el servidor aborta cada corrida con BlockingError.
        ( cd "$RAIZ" && "$cli" dev --host 127.0.0.1 --port "$PUERTO_STUDIO" --allow-blocking )
        ;;
    estado)   confirmar_entorno; mostrar_estado ;;
    stop)     detener_todo ;;
    limpiar)
        confirmar_entorno
        ( cd "$RAIZ" && "$PY" -c "from src.agente import db; db.limpiar(); print('Bandeja vacia.')" )
        ;;
    reiniciar)
        # La demo desde cero: archiva los casos (no se reprocesan los correos viejos
        # del buzon) y devuelve los sistemas a la semilla.
        confirmar_entorno
        ( cd "$RAIZ" && "$PY" -c "from src.agente import servicio; r = servicio.reiniciar_demo(); print('Demo reiniciada: %d caso(s) archivado(s), sistemas en los datos iniciales.' % r['casos_archivados'])" )
        ;;
    restaurar)
        # El mini sistema legado vuelve a la semilla. No toca la bandeja.
        confirmar_entorno
        ( cd "$RAIZ" && "$PY" -c "from src.sistemas_web import sistemas; sistemas.restaurar(); print('Sistemas restaurados a los datos de la demo.')" )
        ;;
    ayuda|-h|--help) ayuda ;;
    *) rojo "Comando desconocido: $COMANDO"; ayuda; exit 1 ;;
esac
