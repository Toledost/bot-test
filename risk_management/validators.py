"""Validaciones previas a la apertura de una posición."""
from __future__ import annotations

from config.settings import RiskConfig
from core.exceptions import InsufficientBalanceError
from utils.logger import get_logger

logger = get_logger(__name__)


def validate_sufficient_balance(balance: float, required_margin: float) -> None:
    if balance < required_margin:
        raise InsufficientBalanceError(
            f"Balance insuficiente: disponible={balance:.2f}, requerido={required_margin:.2f}"
        )


def validate_max_open_positions(open_positions_count: int, cfg: RiskConfig) -> bool:
    """Retorna True si se permite abrir una nueva posición según el límite configurado."""
    allowed = open_positions_count < cfg.max_open_positions
    if not allowed:
        logger.info(
            "Límite de posiciones abiertas alcanzado (%d/%d). No se abrirán nuevas posiciones.",
            open_positions_count,
            cfg.max_open_positions,
        )
    return allowed
