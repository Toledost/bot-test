"""Excepciones propias del dominio del bot."""
from __future__ import annotations


class BotError(Exception):
    """Excepción base para errores del bot."""


class ExchangeConnectionError(BotError):
    """Fallo persistente de conectividad con el exchange tras agotar reintentos."""


class InsufficientBalanceError(BotError):
    """Saldo insuficiente para ejecutar la operación solicitada."""


class DailyLossLimitReached(BotError):
    """Se alcanzó el límite de pérdida diaria configurado (circuit breaker)."""


class ProtectiveOrderFailure(BotError):
    """Fallo al colocar las órdenes de protección (SL/TP) tras abrir una posición.

    Este error es crítico: una posición sin protección server-side queda expuesta
    a liquidación si el proceso local se detiene. Debe manejarse cerrando la
    posición inmediatamente si la protección no puede garantizarse.
    """
