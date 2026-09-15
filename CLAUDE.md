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

# alternativa si se tiene Python 3.12 instalado localmente:
python scripts/monitor_paper.py
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
                     retorna una señal (LONG/SHORT/CLOSE/HOLD) + ATR. indicators.py
                     contiene EMA/RSI/ATR calculados con pandas puro (sin librerías TA
                     externas). ema_rsi_atr_strategy.py es la estrategia de ejemplo.

risk_management/     position_sizer.py calcula el tamaño de posición para que la pérdida
                     al SL sea un % fijo del balance (RISK_PER_TRADE_PCT). circuit_breaker.py
                     implementa el Daily Loss Limit (congela aperturas si se supera el %
                     de pérdida diaria configurado, resetea en el siguiente día UTC).
                     validators.py valida saldo suficiente y límite de posiciones abiertas.

execution/            OrderExecutor (interfaz ABC) con dos implementaciones:
                     - LiveOrderExecutor: envía órdenes reales, coloca SL/TP server-side
                       (STOP_MARKET / TAKE_PROFIT_MARKET) inmediatamente tras la entrada.
                     - PaperOrderExecutor: simula fills con slippage conservador y fee
                       taker, persiste balance/trades en SQLite (paper_store.py) y en
                       logs/trades_paper.log.

utils/               logger.py configura logging a consola + archivo rotativo
                     (RotatingFileHandler) sobre el volumen /logs montado en Docker.

tests/               Tests unitarios de pytest. Prioridad: position_sizer y
                     circuit_breaker, la lógica más crítica de seguridad de capital.

scripts/             Utilidades operativas fuera del ciclo de vida del bot.
                     monitor_paper.py lee data/trades_paper.db (SQLite) en modo
                     solo-lectura y muestra balance simulado, PnL acumulado y
                     últimos trades, sin interferir con el proceso del bot.
                     Uso recomendado: `docker compose exec trading-bot python
                     scripts/monitor_paper.py` (evita depender de un intérprete
                     Python instalado en el host).

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
   mercado), revisando `logs/trades_paper.log` y el balance simulado en
   `data/trades_paper.db`.
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
