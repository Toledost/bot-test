"""Carga y validación centralizada de configuración desde variables de entorno."""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from dotenv import load_dotenv

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=_ENV_FILE)


class ExecutionMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class MarketType(str, Enum):
    SPOT = "spot"
    SWAP = "swap"


class ConfigError(Exception):
    """Error de configuración inválida o faltante."""


def _get_env(key: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(key, default)
    if required and (value is None or value == ""):
        raise ConfigError(f"Variable de entorno requerida no definida: {key}")
    return value  # type: ignore[return-value]


def _get_float(key: str, default: float | None = None, required: bool = False) -> float:
    raw = _get_env(key, str(default) if default is not None else None, required)
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Variable de entorno {key} debe ser numérica, recibido: {raw!r}") from exc


def _get_int(key: str, default: int | None = None, required: bool = False) -> int:
    raw = _get_env(key, str(default) if default is not None else None, required)
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Variable de entorno {key} debe ser entera, recibido: {raw!r}") from exc


def _get_bool(key: str, default: bool) -> bool:
    raw = _get_env(key, str(default)).strip().lower()
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class ExchangeConfig:
    api_key: str
    api_secret: str
    symbol: str
    market_type: MarketType
    timeframe: str
    leverage: int


@dataclass(frozen=True)
class StrategyConfig:
    ema_fast_period: int
    ema_slow_period: int
    rsi_period: int
    rsi_overbought: float
    rsi_oversold: float
    atr_period: int
    atr_sl_multiplier: float
    atr_tp_multiplier: float
    trend_filter_ema_period: int
    min_volatility_ratio: float
    volatility_sma_period: int


@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade_pct: float
    daily_loss_limit_pct: float
    max_open_positions: int


@dataclass(frozen=True)
class PaperConfig:
    initial_balance: float
    taker_fee_pct: float
    slippage_pct: float
    db_path: str


@dataclass(frozen=True)
class CoreConfig:
    loop_interval_seconds: int
    max_retries: int
    retry_backoff_base_seconds: float
    journal_db_path: str


@dataclass(frozen=True)
class LoggingConfig:
    level: str
    log_dir: str
    max_bytes: int
    backup_count: int


@dataclass(frozen=True)
class Settings:
    execution_mode: ExecutionMode
    dry_run: bool
    exchange: ExchangeConfig
    strategy: StrategyConfig
    risk: RiskConfig
    paper: PaperConfig
    core: CoreConfig
    logging: LoggingConfig

    def __post_init__(self) -> None:
        # Guardia crítica: en modo live, DRY_RUN debe estar explícitamente en false.
        # Evita que una configuración inconsistente (.env mal editado) permita
        # envío de órdenes reales sin intención explícita del operador.
        if self.execution_mode is ExecutionMode.LIVE and self.dry_run:
            raise ConfigError(
                "Configuración inconsistente: EXECUTION_MODE=live pero DRY_RUN=true. "
                "Ambas variables deben coincidir explícitamente para operar en real."
            )
        if self.execution_mode is ExecutionMode.PAPER and not self.dry_run:
            raise ConfigError(
                "Configuración inconsistente: EXECUTION_MODE=paper pero DRY_RUN=false. "
                "Ambas variables deben coincidir explícitamente para operar en paper."
            )


def load_settings() -> Settings:
    """Construye el objeto Settings validado a partir de variables de entorno."""
    execution_mode = ExecutionMode(_get_env("EXECUTION_MODE", "paper").strip().lower())
    dry_run = _get_bool("DRY_RUN", True)

    exchange = ExchangeConfig(
        api_key=_get_env("BINGX_API_KEY", required=True),
        api_secret=_get_env("BINGX_API_SECRET", required=True),
        symbol=_get_env("SYMBOL", required=True),
        market_type=MarketType(_get_env("MARKET_TYPE", "swap").strip().lower()),
        timeframe=_get_env("TIMEFRAME", "15m"),
        leverage=_get_int("LEVERAGE", 3),
    )

    strategy = StrategyConfig(
        ema_fast_period=_get_int("EMA_FAST_PERIOD", 12),
        ema_slow_period=_get_int("EMA_SLOW_PERIOD", 26),
        rsi_period=_get_int("RSI_PERIOD", 14),
        rsi_overbought=_get_float("RSI_OVERBOUGHT", 70),
        rsi_oversold=_get_float("RSI_OVERSOLD", 30),
        atr_period=_get_int("ATR_PERIOD", 14),
        atr_sl_multiplier=_get_float("ATR_SL_MULTIPLIER", 1.5),
        atr_tp_multiplier=_get_float("ATR_TP_MULTIPLIER", 3.0),
        trend_filter_ema_period=_get_int("TREND_FILTER_EMA_PERIOD", 0),
        min_volatility_ratio=_get_float("MIN_VOLATILITY_RATIO", 0),
        volatility_sma_period=_get_int("VOLATILITY_SMA_PERIOD", 20),
    )

    risk = RiskConfig(
        risk_per_trade_pct=_get_float("RISK_PER_TRADE_PCT", 1.0),
        daily_loss_limit_pct=_get_float("DAILY_LOSS_LIMIT_PCT", 2.0),
        max_open_positions=_get_int("MAX_OPEN_POSITIONS", 1),
    )

    paper = PaperConfig(
        initial_balance=_get_float("PAPER_INITIAL_BALANCE", 1000.0),
        taker_fee_pct=_get_float("PAPER_TAKER_FEE_PCT", 0.05),
        slippage_pct=_get_float("PAPER_SLIPPAGE_PCT", 0.05),
        db_path=_get_env("PAPER_DB_PATH", "/data/trades_paper.db"),
    )

    core = CoreConfig(
        loop_interval_seconds=_get_int("LOOP_INTERVAL_SECONDS", 30),
        max_retries=_get_int("MAX_RETRIES", 5),
        retry_backoff_base_seconds=_get_float("RETRY_BACKOFF_BASE_SECONDS", 2),
        journal_db_path=_get_env("JOURNAL_DB_PATH", "/data/journal.db"),
    )

    logging_cfg = LoggingConfig(
        level=_get_env("LOG_LEVEL", "INFO"),
        log_dir=_get_env("LOG_DIR", "/logs"),
        max_bytes=_get_int("LOG_MAX_BYTES", 10 * 1024 * 1024),
        backup_count=_get_int("LOG_BACKUP_COUNT", 5),
    )

    return Settings(
        execution_mode=execution_mode,
        dry_run=dry_run,
        exchange=exchange,
        strategy=strategy,
        risk=risk,
        paper=paper,
        core=core,
        logging=logging_cfg,
    )
