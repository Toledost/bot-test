"""Orquestador del ciclo de vida del bot: conecta estrategia, riesgo y ejecución."""
from __future__ import annotations

import time

import pandas as pd

from config.settings import ExecutionMode, Settings
from core.exceptions import (
    DailyLossLimitReached,
    ExchangeConnectionError,
    InsufficientBalanceError,
    ProtectiveOrderFailure,
)
from core.exchange_client import ExchangeClient
from execution.live_executor import LiveOrderExecutor
from execution.order_executor import OrderExecutor
from execution.paper_executor import PaperOrderExecutor
from risk_management.circuit_breaker import DailyLossCircuitBreaker
from risk_management.position_sizer import PositionSizer
from risk_management.validators import validate_max_open_positions, validate_sufficient_balance
from strategies.base_strategy import BaseStrategy, Signal
from utils.logger import get_logger

logger = get_logger(__name__)

_OHLCV_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


class BotEngine:
    """Coordina el ciclo: fetch de datos -> señal de estrategia -> riesgo -> ejecución."""

    def __init__(self, settings: Settings, strategy: BaseStrategy) -> None:
        self._settings = settings
        self._strategy = strategy

        self._client = ExchangeClient(settings.exchange, settings.core)
        self._executor: OrderExecutor = self._build_executor()
        self._sizer = PositionSizer(settings.risk)

        starting_balance = self._executor.get_balance()
        self._circuit_breaker = DailyLossCircuitBreaker(settings.risk, starting_balance)

        self._running = False

    def _build_executor(self) -> OrderExecutor:
        if self._settings.execution_mode is ExecutionMode.LIVE:
            logger.warning("Inicializando en modo LIVE: las órdenes se enviarán con fondos reales.")
            return LiveOrderExecutor(self._client)

        logger.info("Inicializando en modo PAPER (Dry-Run): sin envío de órdenes reales.")
        return PaperOrderExecutor(self._settings.paper, self._settings.logging.log_dir)

    def start(self) -> None:
        """Prepara el exchange (mercados, apalancamiento) antes de iniciar el ciclo."""
        self._client.load_markets()
        if self._settings.exchange.market_type.value == "swap":
            try:
                self._client.set_leverage(self._settings.exchange.leverage)
            except Exception as exc:  # noqa: BLE001 - no crítico, se continúa con el apalancamiento actual
                logger.warning("No se pudo fijar el apalancamiento (%s). Continuando con el actual.", exc)
        self._running = True
        logger.info(
            "Bot iniciado | modo=%s símbolo=%s timeframe=%s estrategia=%s",
            self._settings.execution_mode.value,
            self._settings.exchange.symbol,
            self._settings.exchange.timeframe,
            self._strategy.name,
        )

    def stop(self) -> None:
        self._running = False
        logger.info("Bot detenido.")

    @property
    def is_running(self) -> bool:
        return self._running

    def run_forever(self) -> None:
        self.start()
        while self._running:
            try:
                self.run_cycle()
            except ExchangeConnectionError as exc:
                logger.error("Ciclo omitido por fallo persistente de conectividad: %s", exc)
            except DailyLossLimitReached as exc:
                logger.warning("Ciclo omitido: %s", exc)
            except InsufficientBalanceError as exc:
                logger.warning("Ciclo omitido por saldo insuficiente: %s", exc)
            except ProtectiveOrderFailure as exc:
                logger.error("Ciclo con fallo crítico de protección SL/TP: %s", exc)
            except Exception:  # noqa: BLE001 - último recurso: el bot nunca debe crashear el proceso
                logger.exception("Error inesperado en el ciclo principal. Continuando en el próximo ciclo.")

            if self._running:
                time.sleep(self._settings.core.loop_interval_seconds)

    def run_cycle(self) -> None:
        """Ejecuta una iteración completa: datos -> señal -> riesgo -> ejecución."""
        balance = self._executor.get_balance()
        self._circuit_breaker.register_balance(balance)

        symbol = self._settings.exchange.symbol
        ohlcv_raw = self._client.fetch_ohlcv(symbol, self._settings.exchange.timeframe)
        ohlcv = pd.DataFrame(ohlcv_raw, columns=_OHLCV_COLUMNS)

        result = self._strategy.evaluate(ohlcv)

        if result.signal in (Signal.HOLD, Signal.CLOSE):
            logger.debug("Sin señal de entrada: %s", result.reason)
            return

        if not self._circuit_breaker.can_open_position():
            raise DailyLossLimitReached(
                f"Límite de pérdida diaria ({self._settings.risk.daily_loss_limit_pct}%) alcanzado."
            )

        open_positions = self._executor.get_open_positions()
        if not validate_max_open_positions(len(open_positions), self._settings.risk):
            return

        ticker = self._client.fetch_ticker(symbol)
        entry_price_hint = float(ticker["last"])

        if result.atr is None:
            logger.warning(
                "La estrategia no produjo ATR; se omite la entrada por falta de dato de volatilidad."
            )
            return

        sizing = self._sizer.calculate(
            balance=balance,
            entry_price=entry_price_hint,
            atr=result.atr,
            atr_sl_multiplier=self._settings.strategy.atr_sl_multiplier,
            atr_tp_multiplier=self._settings.strategy.atr_tp_multiplier,
            is_long=result.signal is Signal.LONG,
        )

        required_margin = (sizing.amount * entry_price_hint) / max(self._settings.exchange.leverage, 1)
        validate_sufficient_balance(balance, required_margin)

        self._executor.open_position(
            symbol=symbol,
            signal=result.signal,
            amount=sizing.amount,
            entry_price_hint=entry_price_hint,
            stop_loss_price=sizing.stop_loss_price,
            take_profit_price=sizing.take_profit_price,
        )
