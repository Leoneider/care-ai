+-
-+
3# Care AI — POC Fase 1 y Fase 2 (copiloto, ejecución y cierre con revisión humana)

Esqueleto funcional en LangGraph que va **desde el buzón de Gmail hasta la decisión del ejecutivo**, siguiendo el flujo del diagrama: Cliente → Care AI → Derivación → Ejecutivo Care.

```
buzón Gmail ──▶ src.gmail_ingesta ──┐                  ┌──▶ src.web (bandeja, :8000)
                                     ▼                  │
                              ┌─── servicio.py ─────────┘
                              │   (tramo 1)      (tramo 2)
                              ▼
correo ──▶ ingesta ──▶ identificar_cliente ──▶ segmentar
                │                                  │
           (duplicado)                      fan-out dinámico (Send)
                │                          ┌───────┴───────┐
               END                    procesar_solicitud  ...  (N ramas)
                                           └───────┬───────┘
                                            ensamblar_caso
                                                   │
                                     ⏸  revision_humana  (interrupt)
                                                   │
                            aplicar_decision  (Fase 2: ejecuta en data/sistemas.db)
                                                   │
                                          verificar_resultado
                                    sin riesgo │         │ con riesgo
                                               │   ⏸ revision_cierre (interrupt)
                                               ▼         ▼
                                              cerrar_caso ──▶ END
```

Los dos tramos son **procesos distintos**. El poller de Gmail corre el primero y
deja el caso congelado; el ejecutivo dispara el segundo desde la UI, minutos u
horas después. Lo único que los une es el `thread_id` y un archivo SQLite.

## Cómo resuelve la empresa un ticket (proceso de negocio)

Esta sección describe el **procedimiento** que sigue Care AI para resolver una
solicitud de un cliente B2B, paso a paso y en términos de negocio. En cada ticket
intervienen tres actores, y cada uno tiene un papel que no se cruza con el de los otros:

| Actor | Qué hace | Qué **no** hace |
|---|---|---|
| **La IA** | Lee el correo, separa las solicitudes, las clasifica, extrae los datos que dio el cliente y elige acciones de una lista cerrada | Consultar sistemas, calcular montos, decidir, ejecutar |
| **Los procedimientos fijos** (funciones deterministas) | Consultan CRM, red, contratos, facturación y mesa de servicio; aplican las reglas del negocio; ejecutan los cambios y los verifican | Interpretar lenguaje natural |
| **El ejecutivo Care** | Aprueba, edita o rechaza; revisa los casos de riesgo antes de cerrarlos | — |

Los procedimientos fijos son el equivalente al **manual de operación** de un
analista de postventa: siempre hacen las mismas consultas, en el mismo orden, con
las mismas reglas. Por eso dos correos iguales reciben siempre el mismo análisis,
y cada conclusión se puede auditar.

### El recorrido completo de un ticket

```
 1. Recibir el correo          ── ¿es nuevo y es de un cliente?
 2. Consultar al cliente (CRM) ── ¿quién escribe y está autorizado?
 3. Entender la solicitud (IA) ── ¿qué pide y con qué datos?
 4. Verificar contra sistemas  ── ¿lo que pide es posible / tiene sustento?     ◀── procedimientos fijos
 5. Proponer el plan (IA)      ── ¿qué acciones del catálogo aplican?
 6. Decisión del ejecutivo     ── aprobar / editar / rechazar                    ◀── humano
 7. Ejecutar                   ── abrir ticket, aplicar cambios, derivar          ◀── procedimientos fijos
 8. Verificar                  ── ¿el cambio quedó realmente en el sistema?       ◀── procedimientos fijos
 9. Cerrar                     ── automático si no hay riesgo; si no, el ejecutivo ◀── regla fija / humano
10. Responder al cliente       ── borrador en Gmail que envía el ejecutivo
```

---

### Paso 1. Recibir el correo

**Cuándo arranca:** cada vez que el barrido del buzón (p. ej. cada 60 s) encuentra
un correo **no leído en la bandeja de entrada** (`in:inbox is:unread`, configurable
con `GMAIL_QUERY`).

**Controles antes de abrir un caso:**

| Control | Regla | Si no pasa |
|---|---|---|
| ¿Ya lo procesamos? | Se busca el identificador único del correo (Message-ID) | Se descarta: nunca hay dos casos por el mismo correo |
| ¿Lo escribimos nosotros? | Si es un enviado, un borrador o viene del propio buzón | Se descarta: evita que nuestra respuesta vuelva a entrar como solicitud nueva |
| Limpieza | Se eliminan citas de correos anteriores y firmas | — (el análisis se hace solo sobre lo nuevo) |

---

### Paso 2. Consultar al cliente en el CRM

**Pregunta de negocio:** *¿Quién nos escribe, de qué cuenta es y tiene autoridad
para pedir cambios?*

**Procedimiento:**

1. Se toma la dirección de correo del remitente.
2. **Búsqueda por contacto exacto:** ¿esa dirección está registrada como contacto de alguna cuenta? Si está, se obtiene la cuenta y si el contacto está **autorizado** o no.
3. **Búsqueda por dominio (respaldo):** si el correo exacto no existe, se busca la cuenta por el dominio (`@empresa.com`). La cuenta se identifica, pero el remitente queda como **contacto NO autorizado**: el dominio dice de qué empresa es, no que esa persona pueda pedir cambios.
4. Si se identificó la cuenta, se traen sus datos: razón social, segmento, estado y **la lista de servicios contratados** (sede, producto, Mbps y nodo de red de cada uno).

**Resultados posibles:**

| Resultado | Qué significa | Consecuencia en el proceso |
|---|---|---|
| Cuenta encontrada, contacto autorizado | Caso normal | Sigue sin penalización |
| Cuenta encontrada por dominio, contacto no autorizado | Alguien de la empresa, pero no registrado como autorizado | Confianza × 0,70 y el caso sube marcado para revisión |
| Cuenta no encontrada | El remitente no es cliente B2B conocido | Cuenta `NO IDENTIFICADA`, confianza × 0,70, **no se ejecuta nada en sistemas** y el caso nunca cierra solo |

---

### Paso 3. Entender la solicitud (IA)

La IA separa el correo en solicitudes independientes (un correo puede pedir un
upgrade **y** reclamar una factura) y a cada una le asigna un tipo, un resumen, el
**fragmento literal** del correo que la sustenta y los datos que el cliente dio.
Cada solicitud sigue su propio procedimiento en paralelo.

| Tipo | Prioridad | Datos que el cliente debe dar |
|---|---|---|
| Falla técnica | 1 (más urgente) | sede, descripción de la falla, hora de inicio |
| Inconsistencia de facturación | 2 | periodo |
| Baja de servicio | 2 | servicio, motivo |
| Ampliación de ancho de banda | 3 | sede, ancho de banda objetivo |
| Otro | 3 | — |
| Cambio de datos de contacto | 4 | contacto nuevo, rol |
| Consulta general | 5 | — |

Si falta un dato obligatorio, no se inventa: queda como **faltante** y más adelante
se le pide al cliente.

---

### Paso 4. Verificar contra los sistemas (procedimientos fijos)

Aquí está el corazón del proceso. Según el tipo de solicitud se corre un
procedimiento de verificación distinto. Cada chequeo termina en **ok**, **alerta**
o **falla**, y algunos son **bloqueantes**: impiden que se proponga la acción que
ejecuta el cambio.

#### 4A. Ampliación de ancho de banda

| # | Consulta | Sistema | Regla de negocio | Resultado |
|---|---|---|---|---|
| 1 | **Ubicar el servicio de la sede** | CRM (servicios de la cuenta) | Se busca la sede que nombró el cliente entre los servicios de la cuenta. Tolera tildes, mayúsculas y palabras de relleno ("la sede de Bogotá" encuentra "Bogota - Sede Chapinero"). Compara palabras completas: "Cali" no encuentra "Calle 100" | No se encuentra → **falla bloqueante**, se detiene la verificación |
| 2 | **Calcular el incremento** | — | Incremento = Mbps pedidos − Mbps contratados hoy. Entiende "500", "500 Mbps", "1.000 Mbps" o "1 Gbps" (= 1000) | Incremento ≤ 0 (o no dijo cuánto) → **alerta bloqueante**: confirmar con el cliente |
| 3 | **Consultar capacidad del nodo** | Red | El nodo que atiende ese servicio debe estar **saludable** y tener libres **al menos** los Mbps del incremento | Nodo saturado o sin capacidad → **falla bloqueante**: requiere Ingeniería |
| 4 | **Revisar el contrato** | Contratos | ¿El contrato permite subir el ancho de banda sin adendum? | Sí → ok. No → alerta (no bloquea, pero el adendum queda a cargo del ejecutivo) |

**Qué se propone según el resultado:**

```
¿Encontré la sede? ── no ──▶ pedir dato / escalar
       │ sí
¿Es un aumento real? ── no ──▶ SOLICITAR_DATO_FALTANTE (confirmar Mbps)
       │ sí
¿El nodo tiene capacidad? ── no ──▶ ESCALAR_A_INGENIERIA (nunca el upgrade)
       │ sí
APLICAR_UPGRADE_ANCHO_BANDA  (+ COTIZAR_UPGRADE solo si el contrato exige adendum)
```

#### 4B. Inconsistencia de facturación

| # | Consulta | Sistema | Regla de negocio | Resultado |
|---|---|---|---|---|
| 1 | **Ubicar la factura** | Facturación | Por número si el cliente lo dio; si no, **por periodo**. Entiende "septiembre", "sept 2026", "09/2026" o "2026-09". Sin año, toma el último mes con ese nombre que ya pasó (en octubre, "la de diciembre" es la del año anterior) | No se encuentra → **falla bloqueante** |
| 2 | **Calcular el neto** | Facturación | Neto = valor facturado − notas crédito ya emitidas sobre esa factura | — |
| 3 | **Comparar con lo pactado** | Contratos | Neto vs. valor mensual pactado en el contrato. Se compara el **neto** para que una factura ya corregida no vuelva a salir como reclamo | Diferencia ≠ 0 → alerta: *el reclamo tiene sustento*. Igual → ok: la factura cuadra |
| 4 | **Buscar tickets abiertos** | Mesa de servicio | ¿La cuenta ya tiene un ticket de facturación abierto (abierto, en proceso o en espera)? | Sí → alerta: asociar en vez de duplicar |

**Qué se propone según el resultado:**

```
¿Encontré la factura? ── no ──▶ SOLICITAR_DATO_FALTANTE / ESCALAR_A_FACTURACION
       │ sí
¿El neto supera lo pactado? ── no ──▶ COMPARAR_FACTURA_VS_CONTRATO (informar que cuadra)
       │ sí
SOLICITAR_NOTA_CREDITO  (+ ASOCIAR_A_TICKET_EXISTENTE si ya había uno abierto)
```

#### 4C. Los demás tipos

Falla técnica, baja de servicio, cambio de contacto, consulta general y otro **no
tienen procedimiento de verificación automático** en la POC. Se marcan con la
alerta *"sin validador: revisión manual completa"* y el ejecutivo los revisa enteros.

---

### Paso 5. Proponer el plan de acción (IA con lista cerrada)

Con los resultados de la verificación, la IA arma un plan de **máximo 4 acciones**,
elegidas **solo** del catálogo del tipo de solicitud:

| Tipo | Acciones permitidas |
|---|---|
| Ampliación | Verificar factibilidad, Aplicar upgrade, Cotizar upgrade, Agendar visita técnica, Solicitar dato faltante, Escalar a Ingeniería |
| Facturación | Comparar factura vs. contrato, Solicitar nota crédito, Asociar a ticket existente, Solicitar dato faltante, Escalar a Facturación |
| Falla técnica | Crear incidente, Consultar estado de red, Agendar visita técnica, Escalar a NOC |
| Cambio de contacto | Actualizar contacto en CRM, Solicitar dato faltante |
| Baja de servicio | Verificar permanencia, Escalar a Retención, Solicitar dato faltante |
| Consulta general | Responder con plantilla, Solicitar dato faltante |
| Otro | Escalar a Supervisor |

Reglas que la IA debe cumplir: si hay un bloqueo, no propone el cambio sino el
escalamiento; si faltan datos, incluye *Solicitar dato faltante*; si todo está bien,
incluye la acción que **ejecuta** el cambio y no solo las que lo preparan. Si
propone algo fuera del catálogo, se descarta y el caso sube marcado en rojo.

**Semáforo de la tarjeta.** Cada solicitud parte de la confianza de la IA y se
penaliza: falla en una verificación × 0,60; faltan datos × 0,85; cliente no
identificado o no autorizado × 0,70. Sube **en rojo** si hay bloqueo, faltan datos,
hubo acciones fuera de catálogo o la confianza queda por debajo de **0,75**.

---

### Paso 6. Decisión del ejecutivo

Ningún ticket se ejecuta sin que un ejecutivo lo apruebe.

| Decisión | Qué pasa |
|---|---|
| **Aprobar** | Se ejecutan todas las acciones propuestas |
| **Editar** (quitar acciones o corregir parámetros) | Se ejecutan solo las acciones que dejó. No puede inventar acciones nuevas |
| **Rechazar** | No se ejecuta nada; el caso se descarta |

Si hay un bloqueo, para aprobar debe marcar *"Apruebo pese al bloqueo"* y escribir
el motivo.

---

### Paso 7. Ejecutar (procedimientos fijos que cambian los sistemas)

Primero, por cada solicitud aprobada se **abre un ticket de seguimiento de Care**
en la mesa de servicio (estado *en proceso*, tema según el tipo: red, facturación o
CRM). Cada acción que se ejecuta deja una nota en ese ticket, y todo cambio queda
en la **bitácora** del sistema con el valor de antes y el de después.

Luego cada acción corre su procedimiento. Regla clave: **los montos y cantidades
salen de los sistemas, no de la IA.** La IA puede sugerir un parámetro, pero el
procedimiento lo recalcula.

| Acción | Procedimiento | Controles del sistema (se defiende solo) | Resultado |
|---|---|---|---|
| **Aplicar upgrade** | Ubica el mismo servicio que se validó, sube sus Mbps al valor pedido y **descuenta el incremento de la capacidad libre del nodo** | Rechaza si no es un aumento, si el nodo no está saludable o si no alcanza la capacidad — aunque la verificación ya lo haya revisado | Ejecutada / error |
| **Emitir nota crédito** | Monto = neto de la factura − valor pactado en el contrato. Emite la nota (numeración `NC-70001`, `NC-70002`…) y el neto queda igual a lo pactado | Rechaza montos ≤ 0 o mayores que el saldo de la factura. Si la factura ya cuadra, no emite nada | Ejecutada / error |
| **Asociar a ticket existente** | Busca un ticket abierto de la cuenta con el mismo tema y le anota el caso | Si no había ninguno, no es error: el seguimiento queda en el ticket de Care | Ejecutada / consultada |
| **Verificar factibilidad** | Relee servicio y nodo e informa capacidad libre vs. incremento | — | Consultada |
| **Comparar factura vs. contrato** | Informa neto, pactado y diferencia | — | Consultada |
| **Cotizar upgrade** | Revisa el contrato: si permite upgrade sin adendum, no hace falta cotizar | Si exige adendum, queda para el ejecutivo | Consultada / manual |
| **Escalar / derivar** | Abre un ticket nuevo para el equipo responsable | — | Derivada |
| **Solicitar dato faltante** | Registra qué datos faltan para incluirlos en la respuesta al cliente | — | Pendiente del cliente |
| Actualizar contacto, Consultar estado de red, Verificar permanencia, Responder con plantilla | Sin procedimiento automático en la POC | — | Manual (la hace el ejecutivo) |

**A qué equipo va cada derivación:**

| Acción | Equipo |
|---|---|
| Escalar a Ingeniería | Ingeniería |
| Escalar a Facturación | Facturación |
| Escalar a NOC, Crear incidente | NOC |
| Escalar a Retención | Retención |
| Escalar a Supervisor | Supervisor |
| Agendar visita técnica | Campo |

Si el cliente no está identificado, **ninguna** acción se ejecuta: todas quedan
como manuales. Si un sistema rechaza una operación, esa acción queda en error y
las demás siguen.

---

### Paso 8. Verificar que el cambio quedó

Que el procedimiento dijera "hecho" no basta: se **vuelve a leer** el sistema.

| Acción ejecutada | Qué se relee | Se confirma si… |
|---|---|---|
| Upgrade | El servicio en el inventario de red | Tiene exactamente los Mbps esperados |
| Nota crédito | La factura en facturación | Su neto quedó igual a lo pactado |
| Asociar a ticket | El ticket previo en la mesa de servicio | Tiene la nota con el número del caso |

---

### Paso 9. Cerrar: automático o con revisión

El caso **cierra solo** únicamente cuando **todas** las acciones quedaron
ejecutadas o consultadas, todas las verificaciones confirmaron el cambio y no hay
ningún motivo de riesgo. Pasa a *En verificación* (segunda revisión del ejecutivo) si:

| Motivo | Por qué necesita ojos humanos |
|---|---|
| Una acción falló o el sistema la rechazó | Hay algo sin resolver |
| Una verificación no confirmó el cambio | El sistema no refleja lo que se hizo |
| Una acción quedó derivada a otro equipo | Depende de otro equipo |
| Falta un dato del cliente | Depende del cliente |
| Una acción es manual | Nadie la hizo todavía |
| Nota crédito mayor a **2.000.000 COP** (`CARE_TOPE_NOTA_CREDITO`) | El sistema lo permite, pero el negocio quiere un segundo par de ojos sobre montos altos |
| Se aprobó pese a un bloqueo | Se tomó un riesgo consciente |
| Remitente sin cuenta | No hay a quién atribuir el caso |

En esa revisión el ejecutivo elige:

- **Cerrar:** los tickets de seguimiento de Care pasan a *resuelto*.
- **Dejar abierto:** pasan a *en espera*.

Los tickets derivados a otros equipos nunca se tocan: son de ellos.

---

### Paso 10. Responder al cliente

Con `CARE_BORRADOR_GMAIL=1`, al resolverse el caso (salvo rechazo) se redacta la
respuesta y se deja **como borrador** en el hilo de Gmail: el ejecutivo la revisa y
la envía. La respuesta cuenta solo lo ejecutado y verificado, con las cifras exactas
del sistema; lo que quedó en curso lo dice como en curso, sin plazos ni precios; y
pide por su nombre los datos que faltan. Si el caso cerró, el hilo se etiqueta
`CareAI/Resuelto`.

---

### Ejemplo real con la cuenta de la demo

**Cuenta:** UFPSO · `B2B-005130` · contacto autorizado `ltrigosg@ufpso.edu.co`.

> *"Necesitamos ampliar el ancho de banda de la sede de Bogotá a 500 Mbps […] la
> factura de septiembre llegó por un valor distinto al pactado en el contrato."*

| Paso | Solicitud 1: ampliación | Solicitud 2: facturación |
|---|---|---|
| Consultar cliente | Contacto exacto encontrado, autorizado | ← mismo cliente |
| Ubicar | "Bogotá" → SVC-9310, *Bogota - Sede Chapinero*, 200 Mbps, nodo BOG-N21 | "septiembre" → 2026-09 → factura FE-91502 por 7.150.000 COP |
| Calcular | Incremento 500 − 200 = **300 Mbps** | Neto 7.150.000 (sin notas previas) |
| Consultar | Nodo BOG-N21: saludable, 800 Mbps libres ≥ 300 → **ok** | Contrato CTO-3107 pacta 6.200.000 → diferencia **950.000** → reclamo con sustento |
| Contrato / tickets | CTO-3107 permite upgrade sin adendum → ok | Sin tickets abiertos de facturación |
| Plan propuesto | Aplicar upgrade | Solicitar nota crédito |
| Ejecutar | Ticket INC-56002; SVC-9310 queda en 500 Mbps; nodo queda con 500 libres | Ticket INC-56001; nota NC-70001 por 950.000; neto 6.200.000 |
| Verificar | SVC-9310 tiene 500 Mbps → confirmado | FE-91502 neto 6.200.000 → confirmado |
| Cerrar | Sin motivos de riesgo (950.000 < tope 2.000.000) → **cierre automático**, tickets *resuelto* | ← |

**El mismo correo, pero para la sede de Cúcuta:** SVC-9311 corre sobre el nodo
CUC-N07, *saturado* con 30 Mbps libres → **falla bloqueante** → la IA no puede
proponer el upgrade y propone *Escalar a Ingeniería*. Al aprobar se abre un ticket
para Ingeniería (derivada) y el caso **no cierra solo**: pasa a *En verificación*.

## Correr ahora mismo

Requiere **Python 3.11 o superior**: las librerías de Google dejan de publicar
actualizaciones para 3.10 el 2026-10-04.

```bash
py -3.12 -m venv .venv && .venv/Scripts/activate   # o python3.12 -m venv en Unix
pip install -r requirements.txt
cp .env.example .env                     # y rellena lo que uses

CARE_FAKE_LLM=1 python -m src.demo       # sin API key, respuestas simuladas
pytest -q                                # 99 pruebas
```

Para ver el flujo real, con el humano de verdad en el medio — dos terminales:

```bash
# 1) deja un caso esperando revisión
CARE_FAKE_LLM=1 python -m src.demo --solo-ingesta

# 2) resuélvelo desde la bandeja
python -m src.web                        # http://127.0.0.1:8000

# 3) opcional: mira lo que cambió en los sistemas (otra app)
python -m src.sistemas_web               # http://127.0.0.1:8002
```

## Levantarlo en local

```powershell
.\care.cmd up          # bandeja (:8000) + consola de sistemas (:8002) + poller de Gmail
.\care.cmd sistemas    # solo la consola del sistema legado
.\care.cmd estado      # qué corre y qué hay pendiente
.\care.cmd stop        # para todo
```

`care.cmd demo` siembra el correo de ejemplo sin API key, y `care.cmd limpiar`
vacía la bandeja.

El script existe por dos tropiezos que cuestan un rato la primera vez:

- **`python -m src.web` toma el Python del PATH**, que en una máquina con varias
  versiones suele ser el del sistema y no tiene las dependencias. El script usa
  siempre el del venv, y lo crea si falta.
- **Un servidor viejo se queda con el puerto.** El nuevo no arranca, pero el
  navegador sigue respondiendo — con el código de antes y sin dar ningún error.
  Perseguir ese fantasma es peor que el bug que estabas buscando.

Todo escucha en `127.0.0.1`. La UI no tiene autenticación y por la bandeja pasan
datos de clientes, así que exponerla pide antes login y registrar quién decide.
Si además la app OAuth sigue en modo *Testing* en Google Cloud, el refresh token
**caduca a los 7 días** y el poller se detiene solo; para un piloto real hay que
publicar la app o usar una cuenta de servicio con delegación.

Con modelo real:

```bash
export ANTHROPIC_API_KEY=...
python -m src.demo
```

## Las cinco decisiones que definen la POC

**1. Un correo puede traer varias solicitudes.** Es el caso exacto del enunciado (ampliación de ancho de banda + inconsistencia de facturación en el mismo correo) y es donde casi toda automatización de correo falla. Por eso el nodo `segmentar` devuelve una lista y el grafo hace **fan-out dinámico con `Send`**: una rama paralela por solicitud, sin saber de antemano cuántas son. Cada rama escribe en `solicitudes`, que tiene un reducer aditivo (`operator.add`).

**2. El LLM entiende, el código verifica.** El modelo segmenta, clasifica y extrae datos. Las validaciones contra CRM, facturación, red y contratos son funciones deterministas en `validaciones.py`. Un LLM que "consulta la capacidad del nodo" inventa números; una función no. Esto además hace el caso auditable: cada validación es `ok / alerta / falla`, con detalle y bandera de bloqueante.

**3. Acciones de catálogo cerrado.** `prompts.py` define qué acciones existen por tipo de solicitud. El modelo elige de esa lista; si propone algo fuera del catálogo, se descarta y el caso sube marcado en rojo. Es el guardarraíl más barato y el que más falsos positivos evita.

**4. Toda salida del modelo es tipada.** Nada de parsear texto libre. `schemas.py` tiene los modelos Pydantic y se usa `with_structured_output`. Eso permite comparar contra un golden set desde el día 1.

**5. El humano no es un paso opcional, es el final del grafo.** `revision_humana` usa `interrupt()`: el grafo se congela en el checkpointer y se reanuda con `Command(resume=...)` cuando el ejecutivo decide — pudiendo ser horas después y desde otro proceso. Eso es lo que lo vuelve un copiloto real y no un script que corre de un tirón.

## Qué ve el ejecutivo Care

El payload del `interrupt` es la tarjeta de la bandeja: cuenta, prioridad, confianza global, y por cada solicitud el resumen, el **fragmento literal del correo que lo sustenta**, los datos extraídos, los que faltan, las validaciones y las acciones propuestas. Salida real del demo:

```
[s2] facturacion_inconsistencia  (confianza 0.88)
  Fuente  : "la factura de septiembre llego por un valor distinto al pactado"
  Validaciones:
    [OK  ] factura_identificada: FE-91044 periodo 2026-09, valor 9,720,000 COP
    [WARN] factura_vs_contrato: Diferencia de 1,270,000 COP frente al pactado
    [WARN] ticket_duplicado: Ya existe INC-55120 abierto por facturacion
  Acciones propuestas:
    1. (FACTURACION) COMPARAR_FACTURA_VS_CONTRATO
    2. (CRM) ASOCIAR_A_TICKET_EXISTENTE
```

El `texto_fuente` no es adorno: es lo que permite al ejecutivo verificar en dos segundos en vez de releer el correo. Sin eso, el copiloto agrega trabajo en vez de quitarlo.

## La bandeja del ejecutivo

```bash
python -m src.web            # http://127.0.0.1:8000
```

HTML renderizado en servidor con FastAPI y Jinja. Sin JS, sin build, sin
framework: son tres plantillas y una hoja de estilo. La lista muestra los threads
congelados en `revision_humana` ordenados por prioridad del tipo de solicitud y,
dentro de ella, por antigüedad. Al abrir uno se ve la misma tarjeta que imprime
el demo, pero con las acciones editables.

**La edición se detecta, no se declara.** Si el ejecutivo desmarca una acción o
cambia un parámetro y pulsa *Aprobar*, la decisión se registra como `editar` con
las acciones finales. Un botón que dice aprobar guardando algo distinto de lo
propuesto arruina justo la métrica que el feedback existe para medir: cuántos
casos pasan sin que el humano toque nada. Comprobado por HTTP real:

```
POST /caso/care-3ecba381a5c7f5ef/decidir
     decision=aprobar   sel=s2::0  sel=s1::0  sel=s1::1     ← 3 de las 4 propuestas

decision registrada : editar
acciones ejecutadas : COMPARAR_FACTURA_VS_CONTRATO, COTIZAR_UPGRADE, SOLICITAR_DATO_FALTANTE
```

Lo que **no** es editable: el `codigo` y el `sistema` de cada acción. El catálogo
cerrado de `prompts.py` es el guardarraíl principal de la POC, y una UI que deje
escribir códigos libres lo anula. Se quitan acciones y se corrigen parámetros; no
se inventan acciones nuevas.

**Las validaciones bloqueantes imponen fricción, no prohibición.** `validaciones.py`
marca algunos chequeos como `bloqueante` — un upgrade contra un nodo saturado, por
ejemplo. Pintar una píldora roja no basta: compite con 40 casos en la bandeja y
pierde, y el caso se aprueba por inercia. Para aprobarlo hay que marcar una casilla
y escribir el motivo:

```
⚠  1 validacion(es) bloqueante(s) en este caso
     capacidad_nodo · Nodo MED-N03 en estado 'saturado' con 50 Mbps libres.
   ☐ Apruebo pese al bloqueo, bajo mi criterio. El comentario es obligatorio.
```

Sigue siendo un copiloto: el humano puede aprobarlo, pero no sin verlo. Y el motivo
escrito es lo que después permite distinguir un bloqueo bien puesto de uno mal
calibrado. **Rechazar no pide confirmación**: es la salida segura, y ponerle
fricción empujaría justo hacia aprobar. Si la validación falla, el formulario vuelve
con las ediciones intactas — un guardarraíl que además borra el trabajo deja de
usarse.

`servicio.resolver` rechaza reanudar un thread que ya no tiene `interrupt`, así
que el doble submit y el botón Atrás no reejecutan el grafo desde el principio.

No hay autenticación y está atada a `127.0.0.1`. Antes de exponerla hace falta
login y registrar **quién** decidió cada caso — hoy la columna no existe.

## La ingesta del buzón

```powershell
.\care.cmd gmail                             # guia el setup, autoriza y comprueba
```

```bash
python -m src.gmail_ingesta --autorizar     # una vez, abre el navegador
python -m src.gmail_ingesta --diagnostico   # contra que buzon, y cuantos entrarian
python -m src.gmail_ingesta --dry-run       # normaliza e imprime, no corre el grafo
python -m src.gmail_ingesta                 # un barrido
python -m src.gmail_ingesta --loop 60       # cada 60 s
```

`--diagnostico` existe porque, sin el, el primer barrido real es a la vez la
primera prueba de la autenticacion: si falla no se sabe si fue el OAuth, la query
o el grafo. Dice contra que buzon esta, con que scope, cuantos mensajes trae la
query y si `CARE_CORREO_DEMO` esta puesta.

Setup, una sola vez: en [console.cloud.google.com](https://console.cloud.google.com)
habilita la **Gmail API**, crea un ID de cliente OAuth de tipo *App de escritorio*
y descarga el JSON como `credentials.json` en la raíz. `--autorizar` abre el
navegador y deja un `token.json` con el refresh token. Ambos archivos están en el
`.gitignore`: quien los tenga lee el buzón.

Se eligió la API sobre IMAP porque da el `threadId` nativo, permite etiquetar lo
ya procesado, y el día que el volumen justifique push por Pub/Sub solo cambia el
disparador — `a_correo` y el grafo no se enteran.

**Tres detalles que no se ven pero deciden si la ingesta aguanta un buzón real:**

- **El cuerpo.** Se prefiere `text/plain`; si el cliente mandó solo HTML se
  degrada a texto. Muchos clientes corporativos mandan solo HTML, y sin esa
  degradación el cuerpo que ve el modelo llega vacío y el caso sale sin
  solicitudes. El `charset` declarado en la parte MIME se respeta, porque
  `ampliación` en latin-1 leído como UTF-8 no es el mismo prompt.
- **La idempotencia son tres capas y hacen falta las tres.** La query excluye lo
  etiquetado, para no bajar lo mismo cada minuto; `db.reservar` corta por
  Message-ID *antes* de arrancar el grafo, que es lo que salva de dos barridos
  solapados; y el nodo `ingesta` lo vuelve a consultar para quien entre por otra
  puerta. La etiqueta sola no basta: marcarla puede fallar después de procesar.
- **Un thread de LangGraph por correo, no por hilo de Gmail.** Se deriva del
  Message-ID. Reusar el `threadId` haría que el segundo correo de una
  conversación pisara el checkpoint del primero, que es peor que no tener hilos.
  El `threadId` se guarda como metadato para cuando se aborden conversaciones
  multi-correo.

Si el tramo 1 falla a mitad, la reserva se libera y el siguiente barrido lo
reintenta. Sin eso, un 503 del modelo perdería el correo para siempre: quedaría
marcado como visto sin haber dejado caso.

## El borrador de respuesta al cliente

El catálogo incluye acciones con `sistema: CORREO` — sobre todo
`SOLICITAR_DATO_FALTANTE` — y hasta ahora morían como `simulada`: el plan decía
*"pedirle al cliente la fecha de activación"* y nadie se la pedía.

Ahora, al aprobar, `servicio.py` redacta la respuesta y la deja como **borrador
dentro del hilo de Gmail**. El ejecutivo lo abre, lo corrige y lo manda él.

```dotenv
CARE_BORRADOR_GMAIL=1     # apagado por defecto: escribe en el buzon
```

Cuatro decisiones:

- **Borrador, no envío.** Conserva el principio de Fase 1 — el humano es el único
  que envía — y evita el modo de fallo caro: un correo automático y equivocado a
  un cliente no se deshace. Como efecto secundario no hay que ampliar el scope:
  `drafts.create` entra en el `gmail.modify` que ya usamos para etiquetar. Enviar
  exigiría `gmail.send`, que deliberadamente no pedimos.
- **El prompt le prohíbe prometer.** En esta fase ninguna acción se ejecutó en los
  sistemas, así que el borrador puede acusar recibo y pedir datos, nunca decir que
  algo "ya quedó gestionado" ni dar plazos o precios.
- **Vive en `servicio.py`, no en el grafo.** `aplicar_decision` no sabe que Gmail
  existe. Si mañana el canal es WhatsApp o un ticket, cambia una función y ni los
  nodos ni las pruebas de topología se enteran.
- **Un fallo aquí no tumba la decisión.** El caso ya se resolvió y se registró. Un
  403 de scope o un modelo caído dejan al ejecutivo sin borrador, no sin caso; el
  estado queda en la bandeja y se ve en la vista del caso resuelto.

Se dispara solo con las acciones **finales**: si el ejecutivo quitó la de pedir la
fecha, el borrador no la pide.

## Partirlo en dos procesos: lo que hubo que cambiar

El punto 5 de arriba decía que el humano es el final del grafo. Mientras todo
corría dentro de `demo.py` eso era una promesa; con la UI y el poller separados
hubo que pagarla:

- **`InMemorySaver` → `SqliteSaver`.** Un `interrupt` guardado en RAM no
  sobrevive al cambio de proceso ni a un reinicio. Era el bloqueo real, no un
  detalle de configuración.
- **La idempotencia dejó de ser un `set()`.** Con un poller de verdad, un set en
  memoria significa reprocesar el buzón entero cada vez que el proceso arranca.
- **Una tabla de bandeja (`db.py`).** El checkpointer sabe reanudar un thread,
  pero no responde *"qué casos hay pendientes, de qué cuenta y con qué
  prioridad"* sin deserializar el estado completo de cada uno. La lista se pinta
  con una consulta; el detalle sí va al checkpointer.
- **Una capa de servicio (`servicio.py`).** Ni la UI ni el poller importan
  `graph`, `nodes` ni `Command`. Si el grafo se parte o el checkpointer pasa a
  Postgres, el cambio se agota en ese archivo.

Todo vive en `data/care.db` — bandeja y checkpoints en el mismo archivo: un
archivo, un backup.

## Ver la ejecución paso a paso

`app.invoke()` devuelve solo el estado final, así que de la corrida no se ve nada.
Para ver el grafo avanzando nodo por nodo hay tracing en LangSmith:

1. Crea una key en [smith.langchain.com](https://smith.langchain.com) → Settings → API Keys.
2. Pégala en `.env`:

```dotenv
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=lsv2_pt_...
LANGSMITH_PROJECT=care-ai
```

3. `python -m src.demo`. Al final de cada tramo se imprime el enlace a la traza.

En LangSmith cada nodo es un span anidado, con su entrada, su salida y su latencia.
Las dos ramas del fan-out aparecen en paralelo y se ve cuál de las dos manda en el
tiempo total:

```
care-ai | Ampliacion sede Bogota + revision factura septiembre     2.1s
├─ ingesta                                                          12ms
├─ identificar_cliente                                               3ms
├─ segmentar                                                        840ms
│  └─ FakeLLM::Segmentacion            (o ChatAnthropic)            838ms
├─ procesar_solicitud  [s1]                                         1.2s   ┐ en
│  ├─ FakeLLM::DatosExtraidos                                       520ms  │ paralelo
│  └─ FakeLLM::PlanAcciones                                         660ms  │ (Send)
├─ procesar_solicitud  [s2]                                         1.3s   ┘
├─ ensamblar_caso                                                    2ms
└─ revision_humana                                          ⏸ interrupt
```

Tres detalles del cableado que no vienen de fábrica:

- **El `.env` no se leía.** No había `load_dotenv()` en ninguna parte, así que esas
  variables nunca llegaban al proceso. Ahora `src/__init__.py` lo carga, y lo hace
  ahí porque corre antes que cualquier submódulo: `llm.py` resuelve `CARE_MODEL` a
  nivel de módulo, y cargarlo más tarde llegaría después de los imports.
- **Sin key, LangSmith no traza y tampoco protesta.** `src/__init__.py` detecta el
  caso, apaga el tracing y lo dice, en vez de dejarte esperando trazas que no van a
  llegar.
- **El modo fake también se traza.** `FakeStructuredLLM` no es un Runnable de
  LangChain, así que LangSmith no lo vería y la traza offline mostraría los nodos
  pero ningún prompt. Va decorado con `@traceable(run_type="llm")` para que la traza
  con `CARE_FAKE_LLM=1` tenga la misma forma que la del modelo real.

Cada `invoke` es una traza distinta: la primera va de la ingesta al `interrupt`, la
segunda arranca cuando el ejecutivo decide. No es un detalle cosmético — en
producción esas dos pueden estar separadas por días y por procesos distintos, y la
traza refleja eso.

## Fase 2: ejecutar, verificar y cerrar

La Fase 1 terminaba en la decisión del ejecutivo y marcaba cada acción como
`simulada`. La Fase 2 cierra el ciclo del ticket: lo aprobado **se ejecuta** en
un mini sistema legado, **se verifica** releyendo ese sistema y el caso **se
cierra solo** si no hay riesgo.

**El mini sistema** (`src/sistemas.py`, base `data/sistemas.db`) reemplaza los
diccionarios de `backends.py`: CRM, facturación, contratos, red y mesa de
servicio, con una bitácora de cada escritura (antes y después). Se defiende solo
como uno real: rechaza un upgrade que no cabe en el nodo o una nota crédito mayor
que la factura. `backends.py` sigue siendo la costura, con las mismas firmas de
lectura y cuatro de escritura nuevas.

**La cuenta de la demo** es `UFPSO (cuenta de pruebas)` · `B2B-005130`, con
`ltrigosg@ufpso.edu.co` como contacto autorizado. Está
preparada para este correo, que recorre todo el camino:

> Necesitamos ampliar el ancho de banda de la sede de Bogota a 500 Mbps […]
> la factura de septiembre llego por un valor distinto al pactado en el contrato.

| Antes | Después del caso |
|---|---|
| SVC-9310, Bogotá, 200 Mbps; nodo BOG-N21 con 800 libres | 500 Mbps; nodo con 500 libres |
| FE-91502 septiembre: 7.150.000 frente a 6.200.000 pactados | Nota crédito NC-70001 por 950.000; neto 6.200.000 |
| Sin tickets | Dos tickets de seguimiento, ambos `resuelto` |

Probado con el modelo real: el correo tal cual se clasifica, se valida, el modelo
propone `APLICAR_UPGRADE_ANCHO_BANDA` y `SOLICITAR_NOTA_CREDITO`, se ejecutan, se
verifican y el caso cierra solo.

**Qué no cierra solo** (pasa a la pestaña *En verificación*): una acción que
falló o que el sistema rechazó, una derivada a otro equipo, una que pide un dato
al cliente, una sin herramienta automática, una nota crédito por encima de
`CARE_TOPE_NOTA_CREDITO` (2.000.000 COP por defecto), un caso aprobado pese a una
validación bloqueante, o un remitente sin cuenta. El ejecutivo ve lo ejecutado y
lo verificado, y decide *cerrar* o *dejar el ticket abierto*.

**Al cerrar**, con `CARE_BORRADOR_GMAIL=1`, la respuesta al cliente es la de
cierre: cuenta lo hecho con las cifras exactas del sistema, y el hilo se etiqueta
`CareAI/Resuelto` (configurable con `GMAIL_LABEL_CIERRE`). Sigue siendo un
borrador: enviarlo es del humano.

**Principio de las herramientas:** los `parametros` que propone el modelo son
pistas, no órdenes. Cuántos Mbps aplicar sale de los datos validados; cuánto
acreditar sale de facturación y contratos.

**Para repetir la demo desde cero:** `.\care.cmd reiniciar`. Vacía Pendientes, En
verificación y Resueltos, y devuelve el mini sistema a la semilla. Los casos se
archivan, no se borran: así el poller no vuelve a meter los correos `CARE-TEST`
viejos del buzón. Para arrancar, se manda un correo nuevo. `.\care.cmd restaurar`
(o *Archivo → Restaurar sistemas…* en la consola) solo restaura los sistemas, sin
tocar la bandeja.

**Probar con otros correos:** la semilla trae una cuenta (UFPSO). Para
ensayar con más remitentes, en la consola de sistemas *Nuevo cliente* da de alta
una cuenta completa: contactos, servicios, contrato y facturas. Trae escenarios
precargados (upgrade que cabe, nodo saturado, factura que cuadra, sin contrato);
solo falta poner tu correo. También se puede agregar un contacto a una cuenta
existente, p. ej. tu Gmail personal en UFPSO. El copiloto reconoce al cliente
por el correo exacto del contacto; si se deja vacío el dominio corporativo, otro
remitente de `gmail.com` no cae en esa cuenta. Lo dado de alta sobrevive a
*Restaurar*: vuelve a su estado inicial (tabla `alta` de `sistemas.db`), salvo
que se desmarque *Conservar los clientes de prueba*.

## El agente y los sistemas son dos componentes

La POC corre **dos aplicaciones web separadas**, a propósito:

| | Care AI (el agente) | Sistemas legados (lo que el agente toca) |
|---|---|---|
| Qué es | Bandeja del ejecutivo: revisar, aprobar, cerrar | Consola de CRM, facturación, contratos, red y tickets |
| Código | `src/web/` | `src/sistemas_web/` |
| Arranque | `python -m src.web` · `.\care.cmd ui` | `python -m src.sistemas_web` · `.\care.cmd sistemas` · `.\care.cmd ventana` (ventana propia, modo app) |
| Puerto | `8000` | `8002` |
| Datos | `data/care.db` + checkpoints del grafo | `data/sistemas.db` (`CARE_SISTEMAS_DB`), con su bitácora |
| Conoce al otro | Solo por `backends.py` (escribir) y un enlace (`CARE_SISTEMAS_URL`) | No: no importa `db`, `servicio` ni el grafo |

En producción esos cinco sistemas son de otros equipos y tienen sus propias
pantallas; el agente no los muestra, los usa. Separarlos en la POC hace que la
demo cuente eso: se aprueba un caso en Care AI y se ve el efecto en *otra*
aplicación, con otra identidad visual, sin que una sepa de la otra. También deja
claro qué hay que reemplazar para ir a producción: `src/sistemas_web/` y
`src/sistemas.py` desaparecen, y `backends.py` pasa a hablar con los sistemas
reales.

Reglas que sostienen la separación:

- **Care AI escribe en los sistemas solo a través de `backends.py`.** Nunca desde
  la UI ni importando `sistemas` directamente.
- **La consola solo importa `sistemas`.** `test_sistemas_web.py` lo comprueba.
- **La bandeja no muestra datos de los sistemas**, más allá de lo que el propio
  caso registró al ejecutar. Para ver el estado de una cuenta, el detalle del
  caso enlaza a la consola (*abrir en el sistema legado ↗*). La URL sale de
  `CARE_SISTEMAS_URL` (por defecto `http://127.0.0.1:8002`).
- **Cada app reinicia lo suyo.** Por facilidad para probar la POC, *Restaurar* en
  la consola trae marcada *También vaciar la bandeja de Care AI*: la consola se lo
  pide a la bandeja por HTTP (`POST /demo/vaciar`, URL en `CARE_BANDEJA_URL`, por
  defecto `http://127.0.0.1:8000`), sin importar su código. Los casos se
  archivan, no se borran. Si la bandeja no contesta, los sistemas se restauran
  igual y se avisa. `.\care.cmd reiniciar` hace lo mismo desde la terminal.

## Archivos

| Archivo | Rol |
|---|---|
| `src/graph.py` | Topología, fan-out y compilación con checkpointer |
| `src/state.py` | `CareState` (global) y `SolicitudState` (rama paralela) |
| `src/nodes.py` | Los 10 nodos, incluidos los dos `interrupt` |
| `src/schemas.py` | Contratos Pydantic de las salidas del modelo |
| `src/validaciones.py` | Chequeos deterministas por tipo |
| `src/prompts.py` | Prompts + catálogo de acciones y datos requeridos |
| `src/backends.py` | La costura con los sistemas: lectura y, en Fase 2, escritura |
| **`src/sistemas.py`** | **Mini sistema legado: CRM, facturación, contratos, red y tickets, con bitácora** |
| **`src/ejecucion.py`** | **Fase 2: herramienta por código, verificación y regla de cierre** |
| `src/fakes.py` | Respuestas canned para correr sin API key |
| `src/demo.py` | Corrida end-to-end con el correo del enunciado |
| `src/tracing.py` | Run raíz nombrado en LangSmith + enlace a la traza |
| `src/__init__.py` | Carga del `.env` y guarda de la config de tracing |
| **`src/servicio.py`** | **Capa de aplicación: lo único que la UI y el poller conocen del grafo** |
| **`src/db.py`** | **Bandeja de casos e idempotencia en SQLite** |
| **`src/gmail_ingesta.py`** | **Buzón de Gmail → `CorreoEntrante`, con OAuth y barrido** |
| **`src/web/app.py`** | **UI del ejecutivo: bandeja, detalle, aprobar / editar / rechazar** |
| **`src/respuesta.py`** | **Redacta el borrador de respuesta al cliente** |
| **`src/web/templates/`** | **`base.html`, `bandeja.html`, `caso.html`** |
| **`src/sistemas_web/`** | **Consola del sistema legado, app aparte en `:8002` (ver "El agente y los sistemas son dos componentes")** |
| `conftest.py` | Aísla las pruebas: BD temporal, checkpointer en RAM, modo fake |
| `care.cmd` + `scripts/care.ps1` | Arranque local: levanta, para y reporta la bandeja, la consola de sistemas y el poller |
| `src/feedback.py` | ⏸ Persistencia en BD. Escrito pero **sin conectar** al grafo |
| `src/metricas.py` | ⏸ Métricas de Fase 2 como consultas. Depende de `feedback.py` |
| `src/migrar_feedback.py` | ⏸ Importa el `feedback.jsonl` a la BD cuando se retome |

Las pruebas (99) van por pieza: `test_grafo.py` la topología, `test_servicio.py`
la costura entre procesos, `test_gmail.py` la normalización MIME sin red,
`test_web.py` la UI contra la app real con `TestClient`, `test_respuesta.py` el
borrador (contra un doble de Gmail, sin salir a la red), `test_fase2.py` la
ejecución, la verificación y el cierre, y `test_sistemas_web.py` la consola del
sistema legado y su independencia de la bandeja.

## Por dónde seguir, en orden

~~1. **Un buzón real.**~~ Hecho: `src/gmail_ingesta.py`, API de Gmail con OAuth.
El `message_id` sigue siendo la clave de idempotencia.

~~4. **La UI del ejecutivo.**~~ Hecho: `src/web/`, con la edición guardando las
acciones finales y no solo la decisión.

1. **Podar los checkpoints intermedios.** El saver ya es SQLite, así que los casos
   sobreviven al reinicio; lo que falta es la política de retención. Medido: ~57 KB
   y 9 checkpoints por correo que nunca se liberan (~56 MB a los mil correos). Hay
   que podarlos cuando el caso llega a `aplicar_decision`: de 9 a 1 es ~85% menos.
   `PostgresSaver` solo hace falta cuando haya más de una instancia escribiendo.
2. **Conectar un backend de verdad.** Empezar por uno solo (el CRM). Las firmas de
   `backends.py` no cambian, así que el grafo no se entera.
3. **Quién decidió.** La bandeja registra la decisión y el comentario, pero no el
   usuario: hoy no hay login. Es lo primero que hace falta para sacar la UI de
   `127.0.0.1`, y también para medir criterio por ejecutivo.
4. **Dataset de evaluación.** `aplicar_decision` ya escribe `feedback.jsonl`:
   propuesto vs. final. Con la UI en uso ese archivo por fin se llena con
   decisiones reales y no sintéticas. Con 100–200 casos revisados tienes la línea
   base para medir tasa de clasificación correcta, acciones aceptadas sin edición,
   y datos faltantes bien detectados. Esa métrica es lo que justifica pasar a Fase 2.
5. **Hilos multi-correo.** Hoy cada mensaje abre su caso. El `gmail_thread` ya se
   guarda en la bandeja, que es la mitad del camino.
6. **Observabilidad.** Ya cableada (ver arriba). Lo que falta es usarla: con el
   modelo real, mirar en qué nodo se va la latencia y qué prompt falló.

7. **Mover el feedback a una BD.** `src/feedback.py`, `src/metricas.py` y
   `src/migrar_feedback.py` ya están escritos (SQLite, sin infraestructura) pero
   **siguen sin conectarse**: `aplicar_decision` escribe el `.jsonl`. Conectarlos
   son dos líneas en `nodes.py`; queda pendiente decidir el momento, y la decisión
   no se tomó al agregar la UI. Lo que sí se arregló son los dos problemas del
   archivo: la ruta ya es relativa a la raíz del proyecto y no al CWD
   (`CARE_FEEDBACK_JSONL`), y `pytest` escribe en un temporal en vez de en el
   dataset real — importaba más ahora, porque las pruebas de la UI y del servicio
   habrían pasado de meter 6 filas sintéticas por corrida a varias decenas.

## Lo que deliberadamente NO hace

- No responde al cliente automáticamente. Redacta un borrador y lo deja en el hilo de Gmail; enviarlo es del humano. No pedimos el scope `gmail.send`.
- No ejecuta en sistemas productivos: ejecuta contra el mini sistema de `data/sistemas.db`. Conectar uno real es reemplazar el cuerpo de la función correspondiente en `backends.py`.
- No cotiza ni ajusta precios: el upgrade cambia el ancho de banda pero no el valor mensual del contrato, y `COTIZAR_UPGRADE` solo informa si el contrato lo cubre.
- No maneja adjuntos ni hilos multi-correo. La ingesta lista los nombres de los adjuntos pero no los descarga, y cada mensaje abre su propio caso. Es el siguiente pliegue de complejidad; agregarlo antes de tener la métrica base solo enturbia la medición.
- La UI no tiene autenticación ni registra quién decidió. Está atada a `127.0.0.1` por eso.
