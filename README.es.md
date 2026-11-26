# arg-trading-bot

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

[English](README.md) | Español

Un bot de trading educativo e independiente del broker para el mercado argentino
(acciones y CEDEARs de BYMA). Corre en **modo paper por defecto**: un mercado
sintético determinista, sin red, sin cuenta y sin credenciales.

Este proyecto se comparte para quien quiera entrar en el trading algorítmico en el
mercado argentino y aprender cómo se arma un bot de este tipo: adaptadores de broker,
conciliación de órdenes, sesiones de mercado, límites de riesgo y una estrategia de
ejemplo. No publica resultados de trading ni afirma que la estrategia funcione; es un
punto de partida para leer, correr, cuestionar y mejorar.

> **Esto no es asesoramiento financiero. El uso es bajo tu propio riesgo.** Este es un
> proyecto de aprendizaje. Nada en este repositorio es una recomendación de compra o
> venta de ningún instrumento. Operar implica riesgo de pérdida; los resultados pasados
> o simulados no garantizan resultados futuros. Quien use este código es el único
> responsable de lo que haga con él, sobre todo en modo live.
>
> **No está afiliado, respaldado ni patrocinado por InvertirOnline (IOL), BYMA ni
> ningún broker.** IOL se menciona solo para describir el primer adaptador de broker.
> Todas las marcas pertenecen a sus dueños.

## Inicio rápido (modo paper, menos de 5 minutos)

Requiere Python 3.10+.

```bash
git clone https://github.com/gabriel-dg/arg-trading-bot.git
cd arg-trading-bot
python -m venv .venv
. .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

argbot paper --out paper-run      # 3 sesiones simuladas, alrededor de un minuto
```

Se obtiene un informe JSON (órdenes, ejecuciones, posiciones abiertas, equity
marcado) y el estado durable completo en `paper-run/`. Se pueden probar otras
semillas y duraciones:

```bash
argbot paper --out paper-run-2 --days 5 --seed 42
argbot watch-positions --state-dir paper-run --once
argbot monitor --state-dir paper-run --once --dry-run --as-of-heartbeat
argbot check-config --config configs/examples/strategy.example.json
pytest -q                         # suite de pruebas offline
```

Las ejecuciones en paper son optimistas (una orden limit se ejecuta por completo en
su precio limit en cuanto el libro lo alcanza). El modo paper sirve para entender la
mecánica, no para estimar retornos.

## Qué hay adentro

```
argbot/
  brokers/
    base.py        protocolos Broker + MarketData, OrderView, AccountView
    paper.py       SyntheticExchange, PaperBroker, PaperMarket (por defecto)
    iol/           adaptador IOL: auth, cliente, lectores de órdenes/cuenta, datos de mercado, presupuesto
  execution.py     libro durable de órdenes, conciliación antes de cada envío, nunca reenvía
  strategy/
    rules.py       reglas puras de entrada y stop (la estrategia de ejemplo)
    candidate.py   sesiones, cotizaciones, límites de riesgo, entradas, stops, recuperación
    daily.py       indicadores diarios (EMA, ATR, caída desde el máximo reciente)
  market.py        validación de cotizaciones e historia diaria, independiente del broker
  exchange.py      calendario de feriados de BCBA y helpers para contar ruedas
  sessions.py      días hábiles de BYMA (calendario de feriados revisado)
  lab/             investigación offline: replay y comparación de perfiles sobre datos CSV
  watch_positions.py  tablero de posiciones en terminal, solo lectura (estado local)
  agents/          monitor técnico opcional (apagado por defecto, solo observa)
  files.py         lecturas locales breves y compartidas para los observadores
  live.py          ejecutor live con habilitación explícita (IOL)
configs/examples/  configuración de estrategia de ejemplo y una lista de tickers de ejemplo
examples/          datasets sintéticos para el laboratorio
tests/             pruebas offline (fixtures sintéticos, símbolos de relleno)
```

## Por dónde empezar

Para ver cómo está armado el bot, conviene leer estos archivos en este orden:

1. `argbot/brokers/base.py`: los contratos `Broker` y `MarketData`, y la forma canónica de una cotización.
2. `argbot/brokers/paper.py`: la implementación por defecto, un mercado sintético en el mismo proceso y sin red.
3. `argbot/execution.py`: el libro durable de órdenes. Concilia la cuenta antes de cada envío y nunca reenvía una orden ya mandada.
4. `argbot/strategy/rules.py`: las reglas puras de entrada y stop de la estrategia de ejemplo, sin I/O de broker.
5. `argbot/strategy/candidate.py`: el loop de sesión que conecta esas reglas con cotizaciones, límites de riesgo, entradas, stops y recuperación.
6. `argbot/cli.py` y `argbot/paper_session.py`: cómo arranca una corrida paper. `argbot/live.py` es el camino live, con habilitación explícita.

`argbot/brokers/iol/` es el primer adaptador real de broker. Se lee mejor cuando ya están claros los contratos y el camino paper.

## La estrategia de ejemplo

La estrategia configurada es un ejemplo entre muchos que se pueden armar cambiando
parámetros o escribiendo reglas nuevas. Cada número en
`configs/examples/strategy.example.json` es una ilustración genérica, no un valor
ajustado ni recomendado.

El ejemplo es una estrategia swing, de compra en el retroceso (buy-the-pullback):

1. **Candidatos**: acciones cuyo último cierre está al menos `caida_umbral` (umbral de caída) por debajo de su máximo de
   las últimas `caida_lookback` (ventana de la caída) ruedas, ordenadas por el tamaño de esa caída.
   La lista corta la arma `filtro_candidatos` (filtro de candidatos).
2. **Entrada**: solo en ventanas fijas de entrada (`entry_windows`, antes de `entry_cutoff`), cuando
   el precio está en la parte baja del rango del día (`mec_rango_dia_max`, posición máxima en el rango del día)
   y en o por debajo del VWAP del día (`mec_vwap_max`, desvío máximo respecto del VWAP). Las compras limit pasivas en el bid son opcionales (`compra_pasiva`, compra pasiva).
   `vwap_filtro_pct` (filtro de VWAP) también puede sacar de la lista corta a los nombres que ya se extendieron por encima del VWAP.
3. **Tamaño y riesgo**: como máximo `max_capital_pct` del capital por posición, como máximo
   `max_concurrent` posiciones, un tope de exposición total, un freno de pérdida diaria, un tope
   opcional por sector y un factor de régimen (`regimen_umbral`, umbral de régimen; `regimen_factor`, factor de régimen)
   que achica las posiciones nuevas cuando pocas acciones locales están en tendencia alcista diaria (los CEDEARs quedan afuera de ese breadth).
4. **Salidas**: un stop de protección a `stop_atr_mult` ATR diarios por debajo de la entrada, después un
   stop trailing tipo chandelier, un tope (`trail_giveback_max`) a cuánto puede retroceder el precio desde el
   máximo de la posición, un stop que sube cuando el precio se alejó una distancia fija por encima de la
   entrada (`lock_ganador_umbral`, umbral del lock ganador; `lock_ganador_frac`, fracción del lock ganador),
   confirmaciones opcionales del stop en sondeos consecutivos (`stop_confirm_polls`), y
   una "fase 1" opcional (`stop_fase1_ruedas`, ruedas de fase 1) en la que solo corre el stop de protección durante las primeras ruedas.

Las claves de configuración están en castellano cuando vienen del vocabulario original de trading; las demás siguen en inglés. La traducción aparece entre paréntesis la primera vez que se usa una clave en castellano, y en las tablas de abajo.

### Claves operativas

| Key | English meaning | Qué hace |
| --- | --- | --- |
| `schema` | schema version | Tiene que ser `1`. Identifica esta forma de configuración. |
| `status` | publication status | En el ejemplo es `candidate_not_authorized_for_live`. También se acepta `synthetic` (el modo paper lo asigna). |
| `broker_order_timezone` | broker order timezone | Zona horaria usada para interpretar marcas de tiempo naive en el detalle de órdenes del broker. Solo se acepta `America/Argentina/Buenos_Aires`. |
| `account_id` | account identity | Tiene que coincidir con la identidad que informa el adaptador del broker, y es la clave del lease de la cuenta. En el ejemplo es `null`; es obligatorio en live y el modo paper lo completa. |
| `external_positions` | external positions | Opcional. Instrumentos de la cuenta que el bot no administra (`{"SIMBOLO": cantidad}`), para que la conciliación los acepte. Se lee al crear la carpeta de estado. |
| `assigned_capital` | assigned capital | Capital usado como base para el tamaño de las posiciones y el tope de exposición. En el ejemplo es `null`; es obligatorio en live y el modo paper lo completa. |
| `fee_reserve` | fee reserve | Fracción reservada en cada compra para comisión, derechos de mercado e IVA. Hay que cargarla con el costo total real del broker. |
| `max_exposure_pct` | maximum exposure | Tope de exposición total como fracción del capital de sizing. |
| `max_daily_loss_pct` | maximum daily loss | Freno de pérdida diaria. Las entradas nuevas se detienen cuando el equity marcado cae esta fracción por debajo de la base del día. |
| `universe` | universe | Símbolos que el bot puede operar. |
| `cedears` | CEDEARs | Símbolos CEDEAR del universo. Quedan afuera de la medición de breadth del régimen. |
| `poll_seconds` | poll interval | Segundos entre rondas regulares. |
| `session_end` | session end | Fin de la sesión del bot (ART). Valores aceptados: `17:00` o `18:00`. Si se omite, el fin es `17:00`. |
| `entry_windows` | entry windows | Horarios (ART) en los que se pueden considerar entradas nuevas, cada uno antes de `entry_cutoff`. |
| `entry_cutoff` | entry cutoff | No hay compras nuevas después de esta hora (ART). |
| `max_quote_age_seconds` | maximum quote age | Las cotizaciones más viejas que esto, o "del futuro", se rechazan para entradas nuevas. |
| `managed_quote_age_seconds` | managed quote age | Tope de antigüedad más holgado al administrar posiciones abiertas (stops). Tiene que quedar entre `max_quote_age_seconds` y `poll_seconds`. |
| `max_spread_pct` | maximum spread | Los spreads anchos bloquean entradas, nunca salidas. |
| `max_scale_change_pct` | maximum scale change | Si el último precio difiere del cierre anterior en más que esto, la cotización se trata como un posible split sin ajustar. |
| `http_session_budget` | HTTP session budget | Máximo de pedidos HTTP por sesión, incluidos lecturas, escrituras y autenticación. |
| `http_management_reserve` | HTTP management reserve | Parte de ese presupuesto que se reserva para que los pedidos ordinarios de datos de mercado se corten antes, y quede margen para órdenes, lecturas de cuenta y cotizaciones prioritarias. |

### Claves de la estrategia

Estas viven dentro del objeto `strategy`.

| Key | English meaning | Qué hace |
| --- | --- | --- |
| `max_capital_pct` | maximum capital per position | Máxima parte del capital del día para cada posición nueva, antes del factor de régimen. |
| `max_concurrent` | maximum concurrent positions | Máximo de posiciones abiertas (más compras en vuelo) al mismo tiempo. |
| `sizing_overshoot_max` | sizing overshoot | El candidato publicado solo acepta `1.0`, que significa que el tamaño se redondea hacia abajo a acciones enteras. |
| `filtro_candidatos` | candidate filter | Cómo se arma la lista corta. El motor puede ordenar por caída (`caida`), exigir tendencia alcista diaria (`bull`), o tomar el universo tal cual (`ninguno`). El validador publicado solo acepta `caida`. |
| `caida_lookback` | drawdown lookback | Ruedas usadas para el máximo de referencia. |
| `caida_umbral` | drawdown threshold | Caída mínima (negativa) del último cierre respecto de ese máximo para que un símbolo califique. |
| `mec_rango_dia_max` | maximum day-range position | En la entrada, el precio tiene que estar como máximo a esta altura del rango [mínimo, máximo] del día (`0.40` es el 40% inferior). |
| `mec_vwap_max` | maximum VWAP deviation | En la entrada, el precio tiene que estar en o por debajo del VWAP por `(1 + esto)`. |
| `vwap_filtro_pct` | VWAP filter | Al armar la lista corta, se descartan los símbolos que ya están esta distancia por encima del VWAP. `<= 0` lo desactiva. No se aplica en la última ventana de entrada. |
| `regimen_umbral` | regime threshold | Si la proporción de acciones locales en tendencia alcista diaria está por debajo de esto, las posiciones nuevas se achican. |
| `regimen_factor` | regime factor | Multiplicador de tamaño cuando el breadth está por debajo de `regimen_umbral`. Con `1.0` el tamaño no cambiaría. |
| `trail_stop_pct` | trailing stop (percent) | Stop trailing de respaldo, como fracción del precio, cuando no hay ATR diario. |
| `trail_atr_mult` | trailing stop (ATR multiple) | Stop trailing tipo chandelier: el máximo desde la entrada menos esta cantidad de ATR diarios. |
| `stop_atr_mult` | protective stop (ATR multiple) | Stop de protección: la entrada menos esta cantidad de ATR diarios. |
| `trail_giveback_max` | maximum give-back from peak | El stop nunca queda más lejos que esta fracción por debajo del máximo de la posición. |
| `lock_ganador_umbral` | winner-lock threshold | Cuando el máximo está al menos esta fracción por encima de la entrada, entra el lock ganador. |
| `lock_ganador_frac` | winner-lock fraction | Fracción de (máximo menos entrada) que el stop pasa a conservar. |
| `stop_confirm_polls` | stop confirmation polls | Sondeos consecutivos por debajo del stop antes de enviar una venta. |
| `stop_fase1_ruedas` | phase-1 sessions | Primeras ruedas después de la entrada en las que solo corre el stop de protección. `0` lo apaga. |
| `compra_pasiva` | passive buy | Si es true, las compras limit se colocan en el bid y no cruzan el spread. |
| `buy_timeout_min` | buy timeout | Minutos después de los cuales se cancela una compra no ejecutada. |
| `sell_timeout_min` | sell timeout | Minutos después de los cuales se cancela una venta no ejecutada. |
| `max_trades_dia` | maximum trades per day | Cuando ya hubo esta cantidad de cierres en el día, se detienen las entradas nuevas. |
| `cooldown_loss_min` | loss cooldown | Minutos durante los cuales un símbolo queda en pausa después de un cierre con pérdida. |

### Particularidades del mercado argentino que contempla

- **Horario de negociación**: la sesión abre a las 10:30 ART y termina en `session_end`,
  que solo acepta `17:00` o `18:00`. El ejemplo usa `18:00`, el cierre vigente de BYMA para
  acciones y CEDEARs a 24 h (conviene chequear el horario de BYMA). La apertura de las 10:30
  y los dos cierres permitidos están fijos en el código (`argbot/strategy/candidate.py`,
  `argbot/live.py`), así que un cambio de horario obliga a editar código. Las compras se
  cancelan 15 minutos antes del cierre; las ventas de stop siguen activas hasta el final.
  Con `17:00`, nadie vigila los stops ni las órdenes abiertas entre las 17:00 y las 18:00,
  aunque el mercado siga operando.
- **Liquidación**: las órdenes se envían a 24 h (T+1). No se usa contado inmediato (CI). El
  libro admite que el broker registre una venta a 48 h o 72 h y que el efectivo llegue más
  tarde.
- **Costos e impuestos**: `fee_reserve` reserva comisión, derechos de mercado e IVA en cada
  compra. Hay que cargarla con el costo total real del broker. Los impuestos (ganancias,
  Bienes Personales) no se modelan.
- **Lotes y unidades**: solo acciones enteras (lote 1 para acciones y CEDEARs); se rechazan las cotizaciones con
  otro tamaño de lote.
- **Liquidez**: los spreads anchos (`max_spread_pct`) y el negocio escaso
  (`entry_liquidity`, operaciones por minuto) bloquean entradas, nunca salidas. `entry_liquidity` es
  opcional y no está en la configuración de ejemplo.
- **Tickers**: símbolos de BYMA, como acciones locales y CEDEARs (`cedears`).
- **Feriados**: el calendario de `argbot/exchange.py` y `argbot/sessions.py` cubre **solo
  2026** (feriados y cierres anunciados durante el año). Hay que actualizarlo a mano todos
  los años con el calendario oficial de BYMA, incluidos los días no laborables con fines
  turísticos. El modo live se niega a arrancar en un día fuera de un año revisado, así que
  desde el 1 de enero de 2027 no arranca hasta que se carguen los feriados de 2027.
- **Higiene de datos**: se rechazan las cotizaciones más viejas que `max_quote_age_seconds` o "del futuro";
  la historia diaria necesita 35 ruedas válidas.
- **Eventos corporativos**: el modo live lee la historia diaria sin ajustar de IOL. Un split
  aparece como un salto entre la cotización y el último cierre (`max_scale_change_pct`); se
  **detecta y se bloquea, no se ajusta**: se rechazan las cotizaciones de ese símbolo, así
  que no tiene entradas y su stop no se evalúa sobre el precio inconsistente. Los dividendos
  en efectivo y los cambios de ratio de los CEDEARs tampoco se ajustan y pueden distorsionar
  la caída, el ATR y los stops alrededor de la fecha de corte.

## Agregar un broker (ALyC)

Hay que implementar dos interfaces chicas de `argbot/brokers/base.py`:

- `Broker`: `submit(intent) -> broker_id`, `order(broker_id) -> OrderView`,
  `cancel(broker_id)`, `account() -> AccountView`. Informar **solo** ejecuciones y costos
  **confirmados**; nunca inferir una ejecución a partir de una orden aceptada.
- `MarketData`: `quote(symbol)` con la forma canónica documentada en `base.py`,
  y `history(symbols, session_date, previous_session)`.

`argbot/brokers/paper.py` es el ejemplo completo más chico; `argbot/brokers/iol/` es uno
real. Se aceptan con gusto aportes de otros adaptadores (ver [CONTRIBUTING.md](CONTRIBUTING.md)).
Los adaptadores tienen que respetar los términos de la API de cada broker.

## Laboratorio offline

```bash
argbot lab compare --data examples/synthetic.csv \
  --configs configs/lab/01_reference.json configs/lab/08_combined.json \
  --events examples/synthetic_events.jsonl --out runs/lab-demo
argbot lab validate-settings
argbot lab import-market --help   # convertir CSVs propios de ticks
```

Los datasets incluidos son sintéticos (empresas y eventos de ficción) y no miden
nada sobre mercados reales.

El laboratorio es un **modelo aparte y simplificado**, no la estrategia live, y no tiene
paridad con ella. `lab compare` usa un horario de sesión fijo definido en
`argbot/lab/engine.py` e ignora `entry_windows` y `session_end` de la estrategia;
`configs/bot_settings.json` acepta horarios de entrada solo hasta las 16:40. Los costos
también difieren: alrededor de 0,42% por lado en el laboratorio (0,30% de comisión más
0,05% de derechos de mercado, más IVA), 0,6% en el broker paper y un `fee_reserve` de 0,9%
en la estrategia de ejemplo. Sirve para comparar ideas entre sí, no para predecir qué haría
el bot live.

## Tablero de posiciones (solo lectura)

`argbot watch-positions` muestra en la terminal caja, posiciones abiertas, stops y
valuación del día leyendo un archivo local `execution-state.json`:

```bash
argbot watch-positions --state-dir paper-run --once
argbot watch-positions --state-dir state/live --config configs/local/my.json
```

Solo corre cuando se lo invoca y es estrictamente de solo lectura: nunca escribe
archivos, nunca toma el candado de la cuenta y nunca consulta a ningún broker ni LLM.
`--config` toma por defecto la configuración de ejemplo (sirve para `paper-run`);
para un estado live o propio, conviene pasar el mismo `--config` con el que corrió el
bot para que coincidan el freno diario, el lock ganador y la reserva de comisión.
Los precios que muestra son los últimos que el bot registró en esa carpeta de estado
(precios simulados en corridas paper, no cotizaciones reales ni asesoramiento
financiero).

## Monitor técnico (opcional, apagado por defecto)

`argbot monitor` es un chequeo de salud que solo observa un bot en marcha. Lee el
`execution-state.json` local, calcula señales deterministas (heartbeat viejo, conciliación
sin confirmar, órdenes sin resolver, datos atrasados, presupuesto HTTP, parada anticipada
y otras) y le pide a un modelo, mediante la CLI de Claude Code, un diagnóstico técnico
breve en español. Nunca coloca, cancela ni modifica órdenes, nunca contacta al broker y
nunca reinicia el bot. Los informes quedan en `runs/monitor/<día>/` dentro de la carpeta
del repositorio (ignorado por git), desde cualquier carpeta en que lo corras (con la
instalación editable del inicio rápido): `latest-report.txt`, `alerts.jsonl` para lo que
no esté ok, y una carpeta por chequeo.

Viene apagado. Para activarlo:

1. Instalá Claude Code e iniciá sesión, de modo que `claude` esté en el PATH. El monitor
   usa tu propio login y plan de Claude Code, y su uso cuenta contra ellos.
2. Copiá `configs/examples/monitor.example.json` a `configs/local/monitor.json` y poné
   `"enabled": true`. El resto de los valores es la política revisada (modelo
   `claude-sonnet-5-5`, esfuerzo `low`, un chequeo cada 900 s desde las 10:30 ART, máximo 31
   chequeos, timeout de 120 s, `observe_only`); el monitor se niega a correr si cambia
   alguno.
3. Correlo junto al bot, con el mismo `--config` que usa el bot:

```bash
argbot monitor --state-dir state/live --config configs/local/my.json --policy configs/local/monitor.json
argbot monitor --state-dir paper-run --once --dry-run --as-of-heartbeat   # vista previa, sin modelo
```

`--once` hace un solo chequeo ahora. `--as-of-heartbeat` observa una corrida paper ya
terminada en su propio último heartbeat. `--dry-run` escribe `snapshot.json` (exactamente
lo que se enviaría) y no llama a ningún modelo; funciona sin activarlo. Corre un solo
monitor por cuenta a la vez, incluidos `--once` y `--dry-run`.

Arrancalo después de que el bot haya arrancado en el día. Mientras el estado no registre
la rueda de hoy, el monitor espera sin chequeos ni alertas (`latest-report.txt` lo
indica), y en un día sin rueda termina. Por eso no detecta un bot que nunca arrancó.

**Qué sale de tu computadora.** Solo si está activado y no es `--dry-run`, cada chequeo
envía un prompt a Anthropic mediante la CLI de Claude Code: instrucciones fijas más un
resumen técnico con la hora de observación, fecha y fin de la rueda, la hora desde la que
se cuentan los incidentes nuevos, fase, hora de parada, si el cierre fue normal y si quedó
registrada una parada programada, código de estado, antigüedad del heartbeat y del último
ciclo, intervalo de sondeo, marcas de pausa, código de bloqueo de entradas, antigüedad de
las cotizaciones de las tenencias y de las tenencias valuadas con sus puntas vivas (sin
símbolos), si la valuación está disponible y cuánto lleva un hueco de datos, si la
telemetría del ciclo está bien, lado, estado y antigüedad de las órdenes activas,
resultado, código de motivo y antigüedad de la conciliación, contadores de pedidos HTTP,
cantidades por código de exclusión, códigos de incidentes con su hora y su cantidad, la
marca de la apertura con las cantidades por código de sus incidentes, una nota fija sobre
los límites de la fuente y las señales deterministas. Nunca envía el id de cuenta,
credenciales, símbolos, cantidades, precios, efectivo, patrimonio ni resultados. Los
errores en texto libre se reemplazan por una lista fija de códigos (`redacted_diagnostic`
en otro caso). Un cierre normal verificado se informa sin llamar al modelo. Lo que enviás
se procesa bajo tus propios términos con Anthropic.

**Cómo se acota al modelo.** La CLI corre desde una carpeta temporal vacía, sin
herramientas, servidores MCP, configuración, comandos de barra ni sesión guardada, y
recibe solo una lista corta de variables de entorno (PATH, HOME, carpetas temporales y de
sistema de Windows, `CLAUDE_CONFIG_DIR`): no recibe las credenciales del broker ni una
`ANTHROPIC_API_KEY`. Solo se acepta una respuesta del modelo revisado, con la forma JSON
esperada y sin uso de herramientas. El modelo no puede bajar la severidad de una señal
determinista, y un chequeo fallido se informa como crítico en vez de como bot sano.

**Límites.** Lee solo el estado registrado: no prueba que el proceso siga vivo entre
chequeos ni consulta al broker. Después del fin de la rueda, un bot que murió sin
registrar su parada aparece solo como cierre sin verificar (advertencia). No hay sonido ni
notificación de escritorio; leé `latest-report.txt`. Es una ayuda de diagnóstico, no un
supervisor, y no es asesoramiento financiero.

## Pasar a live

El modo live está apagado salvo que se lo encienda de manera deliberada, y la responsabilidad es de quien lo usa.

0. Chequear que los términos del broker permitan el trading automatizado a través de su API.
1. Copiar `configs/examples/strategy.example.json` a `configs/local/` (ignorado por git) y
   cargar el universo propio, `assigned_capital` y cada parámetro.
2. Poner en `account_id` la identidad que el adaptador de IOL informa para la cuenta:
   `IOL:inversion_Argentina_Pesos:<número de cuenta de IOL>`. Si no coincide, `argbot live`
   se detiene antes de enviar nada y lo avisa.
3. Si la cuenta tiene instrumentos que el bot no debe administrar, listarlos en
   `external_positions` (`{"SIMBOLO": cantidad}`); si no, la conciliación falla. Se lee solo
   cuando se crea la carpeta de estado.
4. Poner `IOL_USERNAME` e `IOL_PASSWORD` en el entorno del proceso (por ejemplo con
   `export` o `$env:`). El bot lee esas variables él mismo; no carga un archivo `.env`.
   Los tokens quedan solo en memoria, salvo que se le pase `token_path` a `IOLAuth` (una
   caché JSON en texto plano; en Windows, `chmod 0600` no tiene efecto). El modo live usa
   siempre la API de producción de IOL; este proyecto no tiene modo sandbox.
5. Arrancarlo y escribir la frase de confirmación cuando se pida. Sin las dos cosas, no se
   envía nada.

   ```bash
   LIVE_TRADING=true argbot live --config configs/local/my.json --state-dir state/live
   ```

   ```powershell
   $env:LIVE_TRADING = "true"; argbot live --config configs/local/my.json --state-dir state/live
   ```

El bot concilia la cuenta antes de cada orden y deja de enviar cuando algo
no coincide. No hay que correr al mismo tiempo otra herramienta que opere la misma cuenta.

**Los stops viven dentro del proceso del bot.** El bot no coloca órdenes stop en el broker,
y no hay supervisor ni reinicio automático (el estado registra una hora `heartbeat_at`;
el `argbot monitor` opcional puede avisar si quedó vieja, pero nada reinicia el bot). Si se detiene el proceso, la
computadora o la conexión, las posiciones abiertas quedan sin stop hasta que el bot vuelva a
correr. Hay que vigilarlo.

## Comentarios y comunidad

Los comentarios en castellano o en inglés son bienvenidos:

- [Discussions](https://github.com/gabriel-dg/arg-trading-bot/discussions) para ideas,
  charlas de estrategia y preguntas.
- [Issues](https://github.com/gabriel-dg/arg-trading-bot/issues) para errores
  (*Bug report*), propuestas de estrategia (*Strategy proposal*) y preguntas (*Question*).
- Problemas de seguridad: ver [SECURITY.md](SECURITY.md).

Nunca compartir en público credenciales, números de cuenta, saldos ni posiciones.

## Licencia

MIT, ver [LICENSE](LICENSE). Notas de terceros en
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
