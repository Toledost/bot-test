"""Cliente de conexión a BingX vía CCXT con rate limiting y reintentos resilientes."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import ccxt
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config.settings import CoreConfig, ExchangeConfig
from core.exceptions import ExchangeConnectionError
from utils.logger import get_logger

logger = get_logger(__name__)

# Excepciones de CCXT consideradas transitorias y por lo tanto reintentables.
_RETRYABLE_EXCEPTIONS = (
    ccxt.NetworkError,
    ccxt.RequestTimeout,
    ccxt.ExchangeNotAvailable,
    ccxt.RateLimitExceeded,
    ccxt.DDoSProtection,
)


def _retry_decorator(core_cfg: CoreConfig) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Genera un decorador tenacity parametrizado con la configuración de reintentos."""
    return retry(
        retry=retry_if_exception_type(_RETRYABLE_EXCEPTIONS),
        stop=stop_after_attempt(core_cfg.max_retries),
        wait=wait_exponential(multiplier=core_cfg.retry_backoff_base_seconds, min=1, max=120),
        before_sleep=before_sleep_log(logger, log_level=30),  # WARNING
        reraise=True,
    )


class ExchangeClient:
    """Envoltorio sobre ccxt.bingx que centraliza rate limiting y reintentos.

    Todas las llamadas de red pasan por aquí para que la política de resiliencia
    (backoff exponencial ante errores transitorios) sea uniforme en todo el bot.
    """

    def __init__(self, exchange_cfg: ExchangeConfig, core_cfg: CoreConfig) -> None:
        self._cfg = exchange_cfg
        self._core_cfg = core_cfg
        self._retry = _retry_decorator(core_cfg)

        self.exchange: ccxt.bingx = ccxt.bingx(
            {
                "apiKey": exchange_cfg.api_key,
                "secret": exchange_cfg.api_secret,
                "enableRateLimit": True,
                "options": {
                    "defaultType": exchange_cfg.market_type.value,
                },
            }
        )

    def load_markets(self) -> dict[str, Any]:
        return cast(dict[str, Any], self._call(self.exchange.load_markets))

    def fetch_ohlcv(
        self,
        symbol: str | None = None,
        timeframe: str | None = None,
        limit: int = 200,
        since: int | None = None,
    ) -> list[list[float]]:
        symbol = symbol or self._cfg.symbol
        timeframe = timeframe or self._cfg.timeframe
        return cast(
            list[list[float]],
            self._call(self.exchange.fetch_ohlcv, symbol, timeframe, since=since, limit=limit),
        )

    def fetch_order_book(self, symbol: str | None = None, limit: int = 20) -> dict[str, Any]:
        symbol = symbol or self._cfg.symbol
        return cast(dict[str, Any], self._call(self.exchange.fetch_order_book, symbol, limit))

    def fetch_ticker(self, symbol: str | None = None) -> dict[str, Any]:
        symbol = symbol or self._cfg.symbol
        return cast(dict[str, Any], self._call(self.exchange.fetch_ticker, symbol))

    def fetch_balance(self) -> dict[str, Any]:
        return cast(dict[str, Any], self._call(self.exchange.fetch_balance))

    def fetch_positions(self, symbols: list[str] | None = None) -> list[dict[str, Any]]:
        symbols = symbols or [self._cfg.symbol]
        return cast(list[dict[str, Any]], self._call(self.exchange.fetch_positions, symbols))

    def set_leverage(self, leverage: int, symbol: str | None = None) -> Any:
        symbol = symbol or self._cfg.symbol
        # BingX exige "side" en swap: BOTH asume modo one-way (posición neta única),
        # coherente con el resto del bot, que no opera en hedge mode.
        return self._call(self.exchange.set_leverage, leverage, symbol, {"side": "BOTH"})

    def create_market_order(
        self, symbol: str, side: str, amount: float, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self._call(self.exchange.create_order, symbol, "market", side, amount, None, params or {}),
        )

    def create_stop_market_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        stop_price: float,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Coloca una orden STOP_MARKET (trigger) registrada server-side en BingX.

        Esta orden vive en los servidores del exchange: si el proceso local
        se cae, pierde internet o el host se apaga, la orden sigue activa y
        protege la posición contra liquidación sin intervención local.
        """
        order_params = {"stopPrice": stop_price, "triggerPrice": stop_price, **(params or {})}
        return cast(
            dict[str, Any],
            self._call(
                self.exchange.create_order,
                symbol,
                "STOP_MARKET",
                side,
                amount,
                None,
                order_params,
            ),
        )

    def create_take_profit_market_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        stop_price: float,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Coloca una orden TAKE_PROFIT_MARKET (trigger) registrada server-side en BingX."""
        order_params = {"stopPrice": stop_price, "triggerPrice": stop_price, **(params or {})}
        return cast(
            dict[str, Any],
            self._call(
                self.exchange.create_order,
                symbol,
                "TAKE_PROFIT_MARKET",
                side,
                amount,
                None,
                order_params,
            ),
        )

    def cancel_order(self, order_id: str, symbol: str | None = None) -> Any:
        symbol = symbol or self._cfg.symbol
        return self._call(self.exchange.cancel_order, order_id, symbol)

    def fetch_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        symbol = symbol or self._cfg.symbol
        return cast(list[dict[str, Any]], self._call(self.exchange.fetch_open_orders, symbol))

    def _call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Ejecuta una llamada CCXT aplicando la política de reintentos con backoff."""
        try:
            return self._retry(func)(*args, **kwargs)
        except _RETRYABLE_EXCEPTIONS as exc:
            logger.error("Fallo persistente de conectividad con el exchange: %s", exc)
            raise ExchangeConnectionError(str(exc)) from exc
        except ccxt.ExchangeError as exc:
            # Errores no transitorios (ej. parámetros inválidos, fondos insuficientes
            # reportados por el exchange): no se reintentan, se propagan tal cual.
            logger.error("Error del exchange (no reintentable): %s", exc)
            raise
