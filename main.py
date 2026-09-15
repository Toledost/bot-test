"""Punto de entrada del bot: inicializa configuración, logging y el ciclo principal."""
from __future__ import annotations

import signal
import sys
from types import FrameType

from config.settings import ConfigError, load_settings
from core.bot_engine import BotEngine
from strategies.ema_rsi_atr_strategy import EmaRsiAtrStrategy
from utils.logger import get_logger, setup_logging

logger = get_logger(__name__)


def _install_signal_handlers(engine: BotEngine) -> None:
    """Registra manejadores de SIGINT/SIGTERM para un apagado ordenado (graceful shutdown)."""

    def _handle_shutdown(signum: int, frame: FrameType | None) -> None:
        logger.info("Señal de terminación recibida (%s). Deteniendo el bot de forma ordenada...", signum)
        engine.stop()

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        # El logger aún no está configurado en este punto: se informa por stderr.
        print(f"Error de configuración: {exc}", file=sys.stderr)
        return 1

    setup_logging(settings.logging)
    logger.info("=" * 80)
    logger.info(
        "Iniciando BingX Trading Bot | modo=%s | dry_run=%s", settings.execution_mode.value, settings.dry_run
    )
    logger.info("=" * 80)

    strategy = EmaRsiAtrStrategy(settings.strategy)
    engine = BotEngine(settings, strategy)
    _install_signal_handlers(engine)

    try:
        engine.run_forever()
    except Exception:  # noqa: BLE001 - último recurso antes de finalizar el proceso
        logger.exception("Error fatal no controlado. El bot se detendrá.")
        return 1

    logger.info("Bot finalizado correctamente.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
