"""Circuit Breaker: congela nuevas aperturas si la pérdida diaria supera el límite."""
from __future__ import annotations

from datetime import UTC, date, datetime

from config.settings import RiskConfig
from utils.logger import get_logger

logger = get_logger(__name__)


class DailyLossCircuitBreaker:
    """Rastrea el PnL diario (UTC) y bloquea nuevas entradas al superar el límite.

    El estado se resetea automáticamente al cruzar la medianoche UTC, de modo
    que el bot vuelve a operar en el siguiente día natural sin intervención manual.
    """

    def __init__(self, cfg: RiskConfig, starting_balance: float, as_of_date: date | None = None) -> None:
        self._cfg = cfg
        self._day_start_balance = starting_balance
        self._current_day = as_of_date if as_of_date is not None else self._utc_today()
        self._tripped = False

    @staticmethod
    def _utc_today() -> date:
        return datetime.now(UTC).date()

    def _maybe_reset_for_new_day(self, current_balance: float, as_of_date: date | None) -> None:
        today = as_of_date if as_of_date is not None else self._utc_today()
        if today != self._current_day:
            logger.info(
                "Nuevo día UTC detectado (%s -> %s). Reseteando circuit breaker de pérdida diaria.",
                self._current_day,
                today,
            )
            self._current_day = today
            self._day_start_balance = current_balance
            self._tripped = False

    def register_balance(self, current_balance: float, as_of_date: date | None = None) -> None:
        """Actualiza el estado del breaker con el balance actual. Llamar en cada ciclo.

        `as_of_date` permite inyectar la fecha "actual" en vez de leer el reloj
        real del sistema — necesario en scripts/backtest.py, donde las velas
        simuladas avanzan por el calendario histórico mucho más rápido (o más
        lento) que el reloj real del proceso. En producción (paper/live) se
        omite y se usa datetime.now(UTC) como siempre.
        """
        self._maybe_reset_for_new_day(current_balance, as_of_date)

        if self._day_start_balance <= 0:
            return

        daily_pnl_pct = ((current_balance - self._day_start_balance) / self._day_start_balance) * 100

        if daily_pnl_pct <= -abs(self._cfg.daily_loss_limit_pct) and not self._tripped:
            self._tripped = True
            logger.warning(
                "CIRCUIT BREAKER ACTIVADO: pérdida diaria %.2f%% supera el límite configurado de %.2f%%. "
                "Nuevas aperturas de posición congeladas hasta el próximo día UTC.",
                daily_pnl_pct,
                self._cfg.daily_loss_limit_pct,
            )

    @property
    def is_tripped(self) -> bool:
        return self._tripped

    def can_open_position(self) -> bool:
        return not self._tripped
