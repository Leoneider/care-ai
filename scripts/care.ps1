<#
    Arranque local de Care AI.

        .\care.cmd up          bandeja + consola de sistemas + poller de Gmail
        .\care.cmd ui          solo la bandeja
        .\care.cmd sistemas    solo la consola del sistema legado (otra app)
        .\care.cmd ventana     la consola en su propia ventana, como programa de escritorio
        .\care.cmd poller      solo la ingesta
        .\care.cmd studio      el grafo en LangGraph Studio (langgraph dev)
        .\care.cmd estado      que corre y que hay en la bandeja
        .\care.cmd stop        para todo
        .\care.cmd limpiar     vacia la bandeja
  .\care.cmd reiniciar   desde cero: bandeja vacia y sistemas en la semilla
  .\care.cmd restaurar   devuelve el mini sistema (CRM, facturacion, red) a la semilla

    Existe por dos tropiezos reales, no por gusto:

    - `python -m src.web` toma el python del PATH, que aqui es el del sistema y
      no tiene las dependencias. El script usa siempre el del venv.
    - Un servidor viejo dejado atras se queda con el puerto, el nuevo no arranca
      y el navegador sigue mostrando codigo antiguo sin dar ningun error.

    Todo escucha en 127.0.0.1: la UI no tiene autenticacion y por la bandeja
    pasan datos de clientes.
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('up', 'ui', 'sistemas', 'ventana', 'poller', 'studio', 'gmail', 'estado', 'stop', 'limpiar', 'restaurar', 'reiniciar', 'ayuda')]
    [string]$Comando = 'ayuda',

    [int]$Puerto = 8000,
    [int]$PuertoSistemas = 8002,
    [int]$Intervalo = 60,
    [int]$PuertoStudio = 2024
)

$ErrorActionPreference = 'Stop'
$Raiz = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Raiz '.venv\Scripts\python.exe'
$DirLogs = Join-Path $Raiz 'data\logs'
$Pids = Join-Path $Raiz 'data\.pids'

function Escribir($texto, $color = 'Gray') { Write-Host $texto -ForegroundColor $color }

# --------------------------------------------------------------------------- #
# Entorno
# --------------------------------------------------------------------------- #
function Confirmar-Entorno {
    if (-not (Test-Path $Py)) {
        Escribir "No hay venv en .venv. Creandolo con Python 3.12..." Yellow
        & py -3.12 -m venv (Join-Path $Raiz '.venv')
        if (-not $?) { throw "No se pudo crear el venv. Instala Python 3.12 o crea .venv a mano." }
        & $Py -m pip install -q --upgrade pip
        & $Py -m pip install -q -r (Join-Path $Raiz 'requirements.txt')
        Escribir "Dependencias instaladas." Green
    }

    # Una comprobacion barata que atrapa el venv a medio instalar.
    & $Py -c "import fastapi, uvicorn, langgraph, googleapiclient" 2>$null
    if (-not $?) {
        Escribir "Faltan dependencias. Instalando..." Yellow
        & $Py -m pip install -q -r (Join-Path $Raiz 'requirements.txt')
    }

    if (-not (Test-Path $DirLogs)) { New-Item -ItemType Directory -Force $DirLogs | Out-Null }
}

function Liberar-Puerto($puerto) {
    $con = Get-NetTCPConnection -LocalPort $puerto -State Listen -ErrorAction SilentlyContinue
    if ($con) {
        $ids = $con | Select-Object -ExpandProperty OwningProcess -Unique
        Escribir "El puerto $puerto estaba ocupado (PID $($ids -join ', ')). Liberandolo." Yellow
        $ids | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Milliseconds 700
    }
}

function Anotar-Pid($nombre, $procesoId) {
    Add-Content -Path $Pids -Value "$nombre=$procesoId" -Encoding utf8
}

# --------------------------------------------------------------------------- #
# Procesos
# --------------------------------------------------------------------------- #
function Iniciar-UI {
    Liberar-Puerto $Puerto
    $log = Join-Path $DirLogs 'ui.log'
    $p = Start-Process -FilePath $Py `
        -ArgumentList @('-m', 'src.web', '--port', $Puerto) `
        -WorkingDirectory $Raiz -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $log -RedirectStandardError "$log.err"
    Anotar-Pid 'ui' $p.Id

    # Esperar a que conteste antes de decir que esta lista: si murio al arrancar,
    # es mejor enterarse aqui que al abrir el navegador.
    $url = "http://127.0.0.1:$Puerto/"
    foreach ($i in 1..25) {
        Start-Sleep -Milliseconds 400
        try {
            Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2 | Out-Null
            Escribir "  bandeja  -> $url" Green
            return $true
        } catch { }
    }
    Escribir "  La UI no respondio. Revisa $log.err" Red
    return $false
}

function Iniciar-Sistemas {
    # La consola del mini sistema legado es otra app, en otro proceso y puerto:
    # el agente y los sistemas que toca no comparten pantalla.
    Liberar-Puerto $PuertoSistemas
    $log = Join-Path $DirLogs 'sistemas.log'
    $p = Start-Process -FilePath $Py `
        -ArgumentList @('-m', 'src.sistemas_web', '--port', $PuertoSistemas) `
        -WorkingDirectory $Raiz -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $log -RedirectStandardError "$log.err"
    Anotar-Pid 'sistemas' $p.Id

    $url = "http://127.0.0.1:$PuertoSistemas/"
    foreach ($i in 1..25) {
        Start-Sleep -Milliseconds 400
        try {
            Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2 | Out-Null
            Escribir "  sistemas -> $url" Green
            return $true
        } catch { }
    }
    Escribir "  La consola de sistemas no respondio. Revisa $log.err" Red
    return $false
}

function Iniciar-Poller {
    $token = Join-Path $Raiz 'token.json'
    if (-not (Test-Path $token)) {
        Escribir "  poller   -> omitido: falta token.json" Yellow
        Escribir "              autoriza una vez:  .\.venv\Scripts\python.exe -m src.poller.gmail_ingesta --autorizar" DarkGray
        return $false
    }
    $log = Join-Path $DirLogs 'poller.log'
    # -u (sin buffer): con la salida redirigida a archivo, Python acumula stdout en
    # bloques de 8KB y el log se ve vacio durante un barrido tras otro. Parece que el
    # poller esta colgado cuando en realidad esta trabajando.
    $p = Start-Process -FilePath $Py `
        -ArgumentList @('-u', '-m', 'src.poller.gmail_ingesta', '--loop', $Intervalo) `
        -WorkingDirectory $Raiz -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $log -RedirectStandardError "$log.err"
    Anotar-Pid 'poller' $p.Id
    Escribir "  poller   -> barriendo Gmail cada $Intervalo s (log: data\logs\poller.log)" Green
    return $true
}

function Detener-Todo {
    $parados = 0
    if (Test-Path $Pids) {
        foreach ($linea in Get-Content $Pids) {
            $procesoId = ($linea -split '=')[1]
            if ($procesoId -and (Get-Process -Id $procesoId -ErrorAction SilentlyContinue)) {
                Stop-Process -Id $procesoId -Force -ErrorAction SilentlyContinue
                $parados++
            }
        }
        Remove-Item $Pids -Force -ErrorAction SilentlyContinue
    }
    # Por si quedo algo de una sesion anterior cuyo .pids se perdio.
    $con = Get-NetTCPConnection -LocalPort $Puerto, $PuertoSistemas -State Listen -ErrorAction SilentlyContinue
    if ($con) {
        $con | Select-Object -ExpandProperty OwningProcess -Unique |
            ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue; $parados++ }
    }
    Escribir "Detenidos: $parados proceso(s)." Green
}

function Mostrar-Estado {
    Escribir "`nProcesos" Cyan
    $con = Get-NetTCPConnection -LocalPort $Puerto -State Listen -ErrorAction SilentlyContinue
    if ($con) { Escribir "  bandeja  arriba  http://127.0.0.1:$Puerto/" Green }
    else { Escribir "  bandeja  parada" DarkGray }

    $con = Get-NetTCPConnection -LocalPort $PuertoSistemas -State Listen -ErrorAction SilentlyContinue
    if ($con) { Escribir "  sistemas arriba  http://127.0.0.1:$PuertoSistemas/" Green }
    else { Escribir "  sistemas parada" DarkGray }

    $poller = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*gmail_ingesta*--loop*' }
    if ($poller) { Escribir "  poller   arriba  (PID $($poller.ProcessId -join ', '))" Green }
    else { Escribir "  poller   parado" DarkGray }

    Escribir "`nBandeja" Cyan
    & $Py -c @"
from src.agente import servicio, db
r = servicio.resumen()
print(f\"  {r['pendientes']} pendiente(s), {r['en_verificacion']} en verificacion, {r['resueltos']} resuelto(s)\")
for c in servicio.listar(db.PENDIENTE)[:10] + servicio.listar(db.EN_VERIFICACION)[:10]:
    marca = 'verif' if c['estado'] == db.EN_VERIFICACION else f\"P{c['prioridad']}   \"
    print(f\"    {marca}  {c['caso_id']:22} {c['razon_social'][:24]:24} {c['asunto'][:40]}\")
"@
    Escribir ""
}

# --------------------------------------------------------------------------- #
# Despacho
# --------------------------------------------------------------------------- #
switch ($Comando) {
    'up' {
        Confirmar-Entorno
        Escribir "`nCare AI" Cyan
        Iniciar-UI | Out-Null
        Iniciar-Sistemas | Out-Null
        Iniciar-Poller | Out-Null
        Escribir "`n  .\care.cmd estado   ver que hay" DarkGray
        Escribir "  .\care.cmd stop     parar todo`n" DarkGray
    }
    'ui' {
        Confirmar-Entorno
        Escribir "`nCare AI" Cyan
        Iniciar-UI | Out-Null
        Escribir ""
    }
    'sistemas' {
        Confirmar-Entorno
        Escribir "`nSistemas legados" Cyan
        Iniciar-Sistemas | Out-Null
        Escribir ""
    }
    'ventana' {
        # La consola en una ventana propia (Edge/Chrome en modo app: sin pestanas ni
        # barra de direcciones), para que en la demo se vea como el programa de
        # escritorio de otro equipo. Si no esta corriendo, se arranca primero.
        Confirmar-Entorno
        $url = "http://127.0.0.1:$PuertoSistemas/"
        $arriba = $true
        try { Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2 | Out-Null } catch { $arriba = $false }
        if (-not $arriba) {
            Escribir "`nSistemas legados" Cyan
            if (-not (Iniciar-Sistemas)) { return }
        }
        $navegador = @(
            "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
            "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
            "$env:ProgramFiles\Google\Chrome\Application\chrome.exe"
        ) | Where-Object { Test-Path $_ } | Select-Object -First 1
        if ($navegador) {
            Start-Process -FilePath $navegador -ArgumentList @("--app=$url", '--window-size=1320,860')
            Escribir "  ventana -> $url" Green
        } else {
            Start-Process $url
            Escribir "  No encontre Edge ni Chrome: se abrio en el navegador por defecto." Yellow
        }
    }
    'poller' {
        Confirmar-Entorno
        Escribir "`nCare AI" Cyan
        Iniciar-Poller | Out-Null
        Escribir ""
    }
    'gmail' {
        Confirmar-Entorno
        # Google lo descarga como client_secret_<id>.apps.googleusercontent.com.json;
        # `ruta_secreto()` en Python acepta ambos nombres, y aqui se busca igual.
        $secreto = Join-Path $Raiz 'credentials.json'
        if (-not (Test-Path $secreto)) {
            $hallado = Get-ChildItem -Path $Raiz -Filter 'client_secret*.json' -File -ErrorAction SilentlyContinue
            if ($hallado) { $secreto = $hallado[0].FullName }
        }
        $token = Join-Path $Raiz 'token.json'

        if (-not (Test-Path $secreto)) {
            Escribir @"

Falta credentials.json, y ese paso es tuyo: hay que crearlo con tu cuenta de
Google. En https://console.cloud.google.com

  1. Crea un proyecto (o elige uno).
  2. Habilita la "Gmail API" desde la biblioteca de APIs.
  3. Configura la pantalla de consentimiento OAuth, tipo Externo, y AGREGATE
     como usuario de prueba. Si te saltas eso, el login falla al final.
  4. Crea credenciales -> ID de cliente de OAuth -> App de escritorio.
  5. Descarga el JSON y guardalo aqui como:

     $secreto

Cuando lo tengas, vuelve a correr:  .\care.cmd gmail

"@ Yellow
            return
        }

        if (-not (Test-Path $token)) {
            Escribir "`nAutorizando. Se abrira el navegador." Cyan
            Escribir "Si dice 'Google no ha verificado esta aplicacion', es tu propia app:" DarkGray
            Escribir "Configuracion avanzada -> Ir a (no seguro).`n" DarkGray
            & $Py -m src.poller.gmail_ingesta --autorizar
            if (-not $?) { Escribir "La autorizacion fallo." Red; return }
        }

        & $Py -m src.poller.gmail_ingesta --diagnostico
    }
    'studio' {
        Confirmar-Entorno
        $cli = Join-Path $Raiz '.venv\Scripts\langgraph.exe'
        & $Py -c "import langgraph_api" 2>$null
        $hayApi = $?
        if (-not $hayApi -or -not (Test-Path $cli)) {
            Escribir "Instalando langgraph-cli[inmem] en el venv..." Yellow
            & $Py -m pip install -q "langgraph-cli[inmem]"
        }
        Liberar-Puerto $PuertoStudio

        # El CLI imprime emojis y la consola de Windows (cp1252) revienta con
        # UnicodeEncodeError antes de arrancar el servidor.
        $env:PYTHONIOENCODING = 'utf-8'
        $env:PYTHONUTF8 = '1'

        Escribir "`nCare AI - LangGraph Studio" Cyan
        Escribir "  api     -> http://127.0.0.1:$PuertoStudio" Green
        Escribir "  studio  -> https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:$PuertoStudio" Green
        Escribir "  Ctrl+C para parar.`n" DarkGray

        # --allow-blocking: los nodos leen SQLite y el mini sistema de forma
        # sincrona; sin la bandera el servidor aborta cada corrida con BlockingError.
        Push-Location $Raiz
        try {
            & $cli dev --host 127.0.0.1 --port $PuertoStudio --allow-blocking
        } finally { Pop-Location }
    }
    'estado' { Confirmar-Entorno; Mostrar-Estado }
    'stop' { Detener-Todo }
    'limpiar' {
        Confirmar-Entorno
        & $Py -c "from src.agente import db; db.limpiar(); print('Bandeja vacia.')"
    }
    'reiniciar' {
        # La demo desde cero: archiva los casos (no se reprocesan los correos viejos
        # del buzon) y devuelve los sistemas a la semilla.
        Confirmar-Entorno
        # Sin comillas dobles dentro del codigo: PowerShell 5.1 parte los \" al pasarlos a un exe.
        & $Py -c "from src.agente import servicio; r = servicio.reiniciar_demo(); print('Demo reiniciada: %d caso(s) archivado(s), sistemas en los datos iniciales.' % r['casos_archivados'])"
    }
    'restaurar' {
        # El mini sistema legado (CRM, facturacion, red, tickets) vuelve a la semilla
        # de la demo. No toca la bandeja: para eso esta 'limpiar'.
        Confirmar-Entorno
        & $Py -c "from src.sistemas_web import sistemas; sistemas.restaurar(); print('Sistemas restaurados a los datos de la demo.')"
    }
    default {
        Escribir @"

Care AI - arranque local

  .\care.cmd gmail       conecta con Google: guia, autoriza y comprueba
  .\care.cmd up          bandeja + sistemas + poller de Gmail
  .\care.cmd ui          solo la bandeja       -> http://127.0.0.1:$Puerto
  .\care.cmd sistemas    solo la consola del sistema legado -> http://127.0.0.1:$PuertoSistemas
  .\care.cmd ventana     la consola en su propia ventana (modo aplicacion de escritorio)
  .\care.cmd poller      solo la ingesta de Gmail
  .\care.cmd studio      el grafo en LangGraph Studio -> http://127.0.0.1:$PuertoStudio
  .\care.cmd estado      que corre y que hay en la bandeja
  .\care.cmd stop        para todo
  .\care.cmd limpiar     vacia la bandeja
  .\care.cmd reiniciar   desde cero: bandeja vacia y sistemas en la semilla
  .\care.cmd restaurar   devuelve el mini sistema (CRM, facturacion, red) a la semilla

Opciones:  -Puerto 8000   -PuertoSistemas 8002   -Intervalo 60   -PuertoStudio 2024

Primera vez:  .\care.cmd gmail   ->   .\care.cmd up

"@
    }
}
