# CLAUDE.md

Guía de contexto y desarrollo continuo para Claude Code en este repositorio.

## Visión general del proyecto

Bot de trading algorítmico para **BingX** (spot y futuros perpetuos), construido en
**Python 3.12**, usando **CCXT** como capa de abstracción del exchange, y ejecutado en
**Docker** de forma local. El bot arranca siempre en **modo Paper Trading (Dry-Run)**
para validar lógica y estabilidad antes de operar con capital real.

Stack tecnológico:
- Python 3.12 (type hints estrictos, `from __future__ import annotations`)
- CCXT (`ccxt.bingx`) para conectividad de mercado y órdenes
- pandas / numpy para cálculo de indicadores técnicos
- tenacity para reintentos con backoff exponencial
- SQLite (stdlib `sqlite3`) para persistencia de paper trading
- Docker / Docker Compose para despliegue local
- pytest, ruff, mypy para calidad de código

## Comandos frecuentes de desarrollo

```bash
# Construir la imagen
docker compose build

# Levantar el bot en segundo plano
docker compose up -d

# Ver logs en vivo
docker compose logs -f trading-bot

# Bajar el bot (detiene el contenedor; docker-compose.yml usa restart: unless-stopped
# mientras esté corriendo, así que un `down` explícito es necesario para detenerlo del todo)
docker compose down

# Reconstruir tras cambios de dependencias y reiniciar
docker compose up -d --build

# Ejecutar localmente sin Docker (requiere entorno virtual con requirements.txt)
python -m venv .venv
source .venv/bin/activate  # En Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py

# Tests
pytest

# Linter y formateo
ruff check .
ruff format .

# Type checking estático
mypy .

# Monitorear balance simulado y trades de paper trading en vivo
# (recomendado: contra el contenedor corriendo, sin depender de Python en el host)
docker compose exec trading-bot python scripts/monitor_paper.py
docker compose exec trading-bot python scripts/monitor_paper.py --watch --interval 15
docker compose exec trading-bot python scripts/monitor_paper.py --last 20

# Analizar el historial de trades cerrados (paper + live + backtest) segmentado por
# condiciones de entrada (RSI, lado, volatilidad) para guiar ajustes de estrategia
docker compose exec trading-bot python scripts/analyze_performance.py
docker compose exec trading-bot python scripts/analyze_performance.py --mode paper
docker compose exec trading-bot python scripts/analyze_performance.py --mode live --symbol BTC/USDT:USDT
docker compose exec trading-bot python scripts/analyze_performance.py --mode backtest

# Backtest: correr la estrategia activa contra velas históricas reales de BingX
# para generar rápidamente el volumen de trades que en vivo tomaría semanas
docker compose exec trading-bot python scripts/backtest.py --days 30
docker compose exec trading-bot python scripts/backtest.py --days 90 --reset  # descarta resultados previos
docker compose exec trading-bot python scripts/backtest.py --days 30 --timeframe 5m

# alternativa si se tiene Python 3.12 instalado localmente:
python scripts/monitor_paper.py
python scripts/analyze_performance.py
python scripts/backtest.py --days 30
```

## Estructura de carpetas y responsabilidades

```
config/             Carga y validación de variables de entorno (.env) vía python-dotenv.
                     Define Settings, ExecutionMode, MarketType y todos los sub-configs
                     tipados (ExchangeConfig, StrategyConfig, RiskConfig, PaperConfig,
                     CoreConfig, LoggingConfig). Única fuente de verdad de configuración.

core/                Conexión con BingX vía CCXT (exchange_client.py), orquestador del
                     ciclo de vida del bot (bot_engine.py) y excepciones de dominio
                     (exceptions.py). Todo el rate limiting y los reintentos con backoff
                     exponencial ante errores transitorios viven en exchange_client.py.

strategies/          BaseStrategy (clase abstracta) define el contrato: recibe OHLCV,
                     retorna un StrategyResult (señal LONG/SHORT/CLOSE/HOLD + ATR +
                     reason + indicators). El campo `indicators` (dict libre) permite
                     que cada estrategia exponga los valores crudos que motivaron la
                     señal (ej. EMA/RSI); se persiste en el journal para análisis
                     posterior (ver TradeJournal más abajo). indicators.py contiene
                     EMA/RSI/ATR calculados con pandas puro (sin librerías TA externas).
                     ema_rsi_atr_strategy.py es la estrategia de ejemplo.

risk_management/     position_sizer.py calcula el tamaño de posición para que la pérdida
                     al SL sea un % fijo del balance (RISK_PER_TRADE_PCT). circuit_breaker.py
                     implementa el Daily Loss Limit (congela aperturas si se supera el %
                     de pérdida diaria configurado, resetea en el siguiente día UTC).
                     validators.py valida saldo suficiente y límite de posiciones abiertas.

execution/            OrderExecutor (interfaz ABC) con dos implementaciones. Ambas, en
                     cada ciclo, primero llaman poll_closed_trade() para detectar si
                     la posición gestionada ya se cerró (por SL o TP) antes de evaluar
                     nuevas señales — sin esto no hay forma de saber si un trade ganó
                     o perdió, así que es el mecanismo que hace posible el journal.
                     - LiveOrderExecutor: envía órdenes reales, coloca SL/TP server-side
                       (STOP_MARKET / TAKE_PROFIT_MARKET) inmediatamente tras la entrada.
                       poll_closed_trade() confirma el cierre vía fetch_positions (no solo
                       mirando si las órdenes SL/TP siguen abiertas, que puede quedar
                       temporalmente desincronizado) y cancela la orden de protección
                       huérfana que haya quedado.
                     - PaperOrderExecutor: simula fills con slippage conservador y fee
                       taker, persiste balance/trades operativos en SQLite (paper_store.py,
                       data/trades_paper.db) y en logs/trades_paper.log. poll_closed_trade()
                       simula el cierre comparando el último precio de ticker contra el
                       SL/TP guardado.

utils/               logger.py configura logging a consola + archivo rotativo
                     (RotatingFileHandler) sobre el volumen /logs montado en Docker.
                     trade_journal.py define TradeJournal: el registro de aprendizaje
                     (data/journal.db) alimentado por AMBOS modos (paper y live) con el
                     contexto completo de cada decisión (indicadores, razón de la señal)
                     y su resultado (PnL, motivo de cierre). Independiente de PaperStore:
                     el journal es historial de solo-análisis, no estado operativo.

tests/               Tests unitarios de pytest. Prioridad: position_sizer,
                     circuit_breaker (seguridad de capital) y paper_executor
                     (detección de cierre SL/TP y registro en el journal).

scripts/             Utilidades operativas fuera del ciclo de vida del bot.
                     - monitor_paper.py: lee data/trades_paper.db (SQLite) en modo
                       solo-lectura y muestra balance simulado, PnL acumulado,
                       últimos trades y el uptime del bot, sin interferir con el
                       proceso. El uptime se calcula leyendo /proc/1/stat dentro del
                       contenedor (asume que el bot es el PID 1, su ENTRYPOINT) — solo
                       funciona corriendo vía `docker compose exec`, no hay estado que
                       el propio bot escriba.
                     - analyze_performance.py: lee data/journal.db y segmenta el
                       historial de trades cerrados (paper y/o live) por condiciones
                       de entrada (RSI, lado LONG/SHORT, motivo de cierre, distancia al
                       SL como proxy de volatilidad) para identificar qué contextos
                       producen mejor o peor resultado. Es una herramienta de lectura
                       humana: no ajusta parámetros por sí sola, informa decisiones
                       manuales de ajuste en .env.
                     - backtest.py: descarga N días de velas históricas reales de BingX
                       y corre la MISMA estrategia + PositionSizer + PaperOrderExecutor
                       de producción vela por vela, generando en minutos el volumen de
                       trades que en vivo tomaría semanas (con TIMEFRAME=15m, un cruce
                       de EMA puede tardar horas/días en aparecer en tiempo real). Escribe
                       en journal.db con execution_mode="backtest" (separado de "paper"/
                       "live", nunca se mezclan) y usa un balance simulado propio
                       (data/backtest_state.db, no toca trades_paper.db). `--reset` borra
                       ambos antes de correr, para que corridas sucesivas con distinta
                       configuración no se acumulen. Costo: ~2 min cada 30 días de
                       velas en 15m (ver "Rendimiento de scripts/backtest.py" más abajo).
                     Uso recomendado para todos: `docker compose exec trading-bot python
                     scripts/<script>.py` (evita depender de un intérprete Python
                     instalado en el host).

main.py              Punto de entrada. Carga settings, configura logging, instancia
                     BotEngine con la estrategia activa, registra manejadores de
                     SIGINT/SIGTERM para graceful shutdown, y arranca el loop principal.
```

## Reglas estrictas de gestión de riesgo (NUNCA romper)

Estas reglas son invariantes de seguridad de capital. Cualquier refactor, nueva
estrategia o nuevo executor debe preservarlas sin excepción:

1. **SL/TP server-side obligatorios en modo live.** Toda apertura de posición en
   `LiveOrderExecutor` DEBE colocar inmediatamente órdenes `STOP_MARKET` y
   `TAKE_PROFIT_MARKET` registradas en BingX tras la orden de entrada. Si la
   colocación de cualquiera de las dos falla, la posición se cierra de inmediato
   (`ProtectiveOrderFailure`) — nunca debe quedar una posición abierta sin protección
   server-side, porque si el host local se apaga o pierde conectividad, el exchange
   sigue protegiendo la cuenta de forma autónoma.

2. **Dimensionamiento por riesgo fijo, nunca por tamaño fijo.** El tamaño de una
   posición siempre se deriva de `RISK_PER_TRADE_PCT` y la distancia al Stop Loss
   (basada en ATR), nunca de un monto fijo en USDT o un % fijo del balance como
   margen. Ver `risk_management/position_sizer.py`.

3. **Circuit breaker de pérdida diaria es de solo bloqueo, nunca de auto-cierre de
   posiciones abiertas.** Al superar `DAILY_LOSS_LIMIT_PCT`, el bot congela nuevas
   aperturas (`can_open_position() -> False`) pero NO cierra posiciones existentes
   (esas ya están protegidas por su propio SL/TP server-side). El breaker resetea
   automáticamente al cruzar medianoche UTC, nunca manualmente ni por reinicio del
   proceso.

4. **`EXECUTION_MODE` y `DRY_RUN` deben coincidir explícitamente.** `Settings.__post_init__`
   valida esto y lanza `ConfigError` si son inconsistentes. Nunca eliminar ni
   relajar esta validación: es la última barrera contra operar en real por accidente.

5. **Ninguna excepción de ciclo debe crashear el proceso.** `BotEngine.run_forever`
   captura explícitamente `ExchangeConnectionError`, `DailyLossLimitReached`,
   `InsufficientBalanceError`, `ProtectiveOrderFailure` y cualquier excepción residual,
   registra el error y continúa en el siguiente ciclo. El bot corriendo en
   `restart: unless-stopped` es la última red de seguridad, no la primera.

6. **Toda llamada de red a CCXT pasa por `ExchangeClient._call`.** Nunca invocar
   `self.exchange.*` de ccxt directamente fuera de `core/exchange_client.py`: ahí es
   donde vive la política uniforme de reintentos con backoff exponencial ante
   `NetworkError`, `RequestTimeout`, `ExchangeNotAvailable`, `RateLimitExceeded`,
   `DDoSProtection`.

7. **El paper trading debe simular fricciones realistas.** `PaperOrderExecutor`
   siempre aplica `PAPER_TAKER_FEE_PCT` y `PAPER_SLIPPAGE_PCT` (slippage en contra
   del trader) sobre cada fill simulado. No optimizar la estrategia contra un
   simulador sin fricciones: los resultados en paper deben aproximar lo real.

8. **El journal de aprendizaje es de solo lectura para la lógica de trading.**
   `TradeJournal` registra contexto y resultados para análisis humano posterior
   (`scripts/analyze_performance.py`); ningún código de `strategies/`,
   `risk_management/` o `core/bot_engine.py` debe leer del journal para decidir
   señales o dimensionar posiciones. Si en el futuro se quiere ajuste automático
   de parámetros a partir del historial, debe ser un mecanismo explícito y
   separado — no una lectura oculta dentro del ciclo de trading — para no
   introducir overfitting silencioso ni curva de aprendizaje impredecible en
   capital real.

## Registro de aprendizaje (journal) y análisis de performance

- **Qué se registra:** cada trade (paper y live) guarda en `data/journal.db` el
  contexto de la decisión de entrada (`signal_reason`, `indicators` — ej. valores
  de EMA/RSI en el momento de la señal, serializados en `indicators_json`) y, al
  cerrarse, el resultado (`exit_price`, `pnl`, `close_reason`: `stop_loss` /
  `take_profit` / `manual`).
- **Detección de cierre:** el bot no recibe un webhook de BingX cuando se ejecuta
  un SL/TP — lo detecta activamente en cada ciclo (`OrderExecutor.poll_closed_trade`,
  llamado al inicio de `BotEngine.run_cycle` antes de buscar nuevas señales). En
  paper se simula comparando el precio actual contra los niveles guardados; en
  live se confirma consultando `fetch_positions`.
- **Limitación conocida:** si el proceso se reinicia mientras hay una posición
  gestionada por el bot abierta, el estado en memoria de `LiveOrderExecutor`
  (`_managed_protective_orders`, IDs de las órdenes SL/TP) se pierde, así que ese
  trade específico podría no cerrarse en el journal aunque BingX sí lo cierre
  correctamente (la protección server-side no depende de este estado, solo el
  registro de aprendizaje). No se implementó recuperación automática de este
  estado tras reinicio — si se necesita, reconstruirlo consultando
  `fetch_open_orders`/`fetch_positions` al arrancar es el punto de partida.
- **Analizar resultados:** `python scripts/analyze_performance.py` segmenta los
  trades cerrados por lado, motivo de cierre, rango de RSI en la entrada y
  distancia al SL (proxy de volatilidad). Usar esto para decidir manualmente si
  ajustar `RSI_OVERBOUGHT`/`RSI_OVERSOLD`, `ATR_SL_MULTIPLIER`/`ATR_TP_MULTIPLIER`,
  etc. en `.env` — **no** hay ajuste automático; con pocos trades por segmento
  (n < ~30) las diferencias pueden ser ruido, no señal real.

## Backtesting (scripts/backtest.py)

Generar decenas de trades en minutos en vez de esperar señales en tiempo real
(con `TIMEFRAME=15m`, un cruce de EMA puede tardar horas o días en aparecer).
Reutiliza `EmaRsiAtrStrategy`, `PositionSizer` y `PaperOrderExecutor` tal cual
están en producción — el backtest valida la estrategia real, no una
reimplementación aparte.

- **Rendimiento:** descargar N días es rápido (~1000 velas/request paginado
  con `since`); lo lento es el bucle de evaluación. La ventana de velas que se
  le pasa a `strategy.evaluate()` en cada paso está acotada a
  `max(min_candles * 3, 200)` — igual que en producción, donde `BotEngine` pide
  `fetch_ohlcv` con límite fijo, nunca todo el historial. **Nunca** pasar la
  ventana completa creciente (`ohlcv.iloc[:i+1]` sin acotar): con miles de velas
  eso vuelve el recálculo de EMA/RSI/ATR efectivamente O(n²) sobre el total,
  pasando de ~2 min/30 días a decenas de minutos para 90 días. Referencia real:
  30 días (2880 velas) ≈ 2 min con la ventana acotada.
- **Fecha simulada, no el reloj real:** tanto `DailyLossCircuitBreaker` como
  `PaperOrderExecutor.open_position`/`close_position`/`poll_closed_trade`
  aceptan un parámetro opcional (`as_of_date` / `timestamp_hint`) para inyectar
  la fecha/hora de la vela histórica que se está procesando. `backtest.py` lo
  pasa siempre; producción (paper/live) lo omite y sigue usando
  `datetime.now(UTC)`. Sin esto, dos bugs reales que ya se dieron: (1) el
  circuit breaker nunca resetea porque "el día siguiente" nunca llega dentro de
  los segundos/minutos que dura la ejecución del script, bloqueando el resto
  del backtest tras la primera pérdida diaria; (2) los timestamps `opened_at`/
  `closed_at` grabados en `trades_paper.db` y en el journal quedan todos con la
  hora real de ejecución del script en vez de la fecha histórica real del
  trade, inutilizando cualquier análisis temporal (aunque el precio/PnL de cada
  trade sí sean correctos, porque esos sí usan los datos de la vela).
- **Resultados van a `execution_mode="backtest"`** en el mismo `journal.db`,
  nunca se mezclan con `"paper"`/`"live"`. `--reset` limpia tanto el balance
  simulado (`data/backtest_state.db`) como las entradas `"backtest"` previas
  del journal, para que corridas sucesivas con distinta configuración
  (`.env` editado entre medio) no queden acumuladas bajo el mismo modo.
- **Simplificación conocida:** la señal se evalúa con las velas hasta la vela
  actual (inclusive) y la entrada se ejecuta al cierre de esa misma vela —
  estándar en backtesting de velas cerradas, pero no idéntica a producción
  (que actúa un ciclo después de que la vela cierra). El SL/TP sí se valida
  contra el high/low real de cada vela posterior (no solo el cierre), evitando
  el sesgo más grave de subestimar cuántas veces se tocó el SL/TP intravela. Si
  una vela toca tanto el SL como el TP, se asume conservadoramente que el SL
  ocurrió primero (sin datos tick a tick no hay forma de saber el orden real).

## Convenciones de estilo

- **PEP 8** vía `ruff check` / `ruff format` (línea máxima 110 caracteres, ver
  `pyproject.toml`).
- **Type hints estrictos** en toda función y método público. `mypy` corre con
  `disallow_untyped_defs = true`. Usar `from __future__ import annotations` en cada
  módulo nuevo.
- **Manejo de excepciones explícito**, nunca `except:` desnudo. Capturar tipos
  específicos de CCXT/dominio; solo el nivel más externo del ciclo (`bot_engine.py`,
  `main.py`) captura `Exception` genérica, y siempre con `logger.exception(...)` para
  preservar el stack trace.
- **Dataclasses inmutables (`frozen=True`)** para value objects de configuración y
  resultados (Settings, StrategyResult, PositionSizeResult, ExecutedTrade).
- **Logging estructurado**, nunca `print()`. Usar `utils.logger.get_logger(__name__)`
  en cada módulo. Los eventos de trading (aperturas/cierres) se registran también en
  `trades_paper.log` en modo paper.
- **Sin dependencias nuevas sin justificación.** Antes de añadir una librería,
  evaluar si pandas/numpy/stdlib ya lo resuelven (ver `strategies/indicators.py`,
  que implementa EMA/RSI/ATR sin librería TA externa).

## Notas operativas sobre BingX

- **El bot asume BingX en modo de posición One-Way, no Hedge Mode.** Todas las
  órdenes (`create_market_order`, `create_stop_market_order`,
  `create_take_profit_market_order`, `set_leverage`) se envían con
  `side: "BOTH"`, el valor que BingX exige en One-Way (posición neta única por
  símbolo). Si la cuenta está en Hedge Mode, BingX rechaza `BOTH` con
  `"In the Hedge mode, the 'Side' field can only be set to LONG, SHORT or ALL"`
  y, simétricamente, rechaza `LONG`/`SHORT` en One-Way con
  `"In the One-way mode, the 'Side' field can only be set to BOTH"`. Verificar
  el modo de posición de la cuenta (BingX → Perpetual Futures → preferencias de
  trading) **antes** de operar en `live`; si se necesita soportar Hedge Mode,
  hay que reintroducir `positionSide` (LONG/SHORT) en cada orden y separar el
  fetch de posiciones por lado — no es soportado en el estado actual del código.
- **`set_leverage` puede fallar de forma benigna** si ya existe una posición
  abierta en ese símbolo (manual o del bot) y el nuevo apalancamiento dejaría
  margen insuficiente para sostenerla (`"Insufficient margin. Please adjust the
  leverage"`). `BotEngine.start()` captura esto como warning no-crítico y
  continúa con el apalancamiento vigente — no es un fallo del bot, es BingX
  protegiendo la posición existente. Se resuelve cerrando esa posición o
  ajustando `LEVERAGE` en `.env` para que coincida con el vigente.
- **`load_markets()` requiere credenciales válidas incluso en modo paper.**
  Es una llamada autenticada de BingX (trae balances/currencies), por lo que
  `BINGX_API_KEY`/`BINGX_API_SECRET` deben ser reales aunque el bot no vaya a
  enviar órdenes.

## Cómo pasar de modo `paper` a `live` de forma segura

1. **No cambiar nunca en caliente.** Detener el bot (`docker compose down`) antes de
   modificar el modo de ejecución.
2. Validar que el bot ha corrido en `paper` de forma estable durante un período
   representativo (recomendado: varias semanas cubriendo distintos regímenes de
   mercado), revisando `logs/trades_paper.log`, el balance simulado en
   `data/trades_paper.db`, y el análisis segmentado de `python
   scripts/analyze_performance.py --mode paper` para confirmar que la
   estrategia no depende de condiciones de mercado poco representativas.
3. Editar `.env`:
   ```
   EXECUTION_MODE=live
   DRY_RUN=false
   ```
   Ambas variables deben cambiar juntas — `Settings.__post_init__` rechaza cualquier
   combinación inconsistente al arrancar.
4. Revisar y ajustar conscientemente en `.env`:
   - `RISK_PER_TRADE_PCT` y `DAILY_LOSS_LIMIT_PCT`: confirmar que reflejan la
     tolerancia real de pérdida del capital que se va a operar.
   - `LEVERAGE` y `MAX_OPEN_POSITIONS`: confirmar que son apropiados para el capital
     real disponible.
   - `BINGX_API_KEY` / `BINGX_API_SECRET`: usar claves con permisos de trading
     habilitados pero **sin** permiso de retiro (withdrawal), como buena práctica de
     seguridad operativa.
   - Confirmar que la cuenta de BingX está en **One-Way Mode** (no Hedge Mode) — ver
     "Notas operativas sobre BingX" arriba. Si está en Hedge Mode, el código
     requiere adaptación antes de operar en real.
5. Empezar con capital real reducido (una fracción pequeña del capital objetivo)
   durante el primer período en `live`, monitoreando activamente `docker compose logs -f`.
6. Reconstruir y levantar: `docker compose up -d --build`.
7. Confirmar en los primeros ciclos, revisando logs, que las órdenes `STOP_MARKET` y
   `TAKE_PROFIT_MARKET` efectivamente aparecen como órdenes abiertas en la cuenta de
   BingX (verificar manualmente en el exchange, no solo confiar en el log local) tras
   cada apertura de posición.
