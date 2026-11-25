"""BCBA market constants and helpers: the holiday calendar, session counting and price ticks.

The holiday list covers 2026 only. Update it every year from BYMA's official calendar.
"""

from __future__ import annotations

from datetime import date, timedelta

# Feriados BCBA 2026 — actualizar cada año
FERIADOS_BCBA: frozenset[date] = frozenset({
    date(2026,  1,  1),   # Año Nuevo
    date(2026,  2, 16),   # Carnaval
    date(2026,  2, 17),   # Carnaval
    date(2026,  3, 24),   # Día de la Memoria
    date(2026,  4,  2),   # Día del Veterano y los Caídos en Malvinas
    date(2026,  4,  3),   # Viernes Santo
    date(2026,  5,  1),   # Día del Trabajador
    date(2026,  5, 25),   # Revolución de Mayo
    date(2026,  6, 15),   # Güemes (trasladado del 17/mié al lunes 15)
    date(2026,  6, 20),   # Paso a la Inmortalidad del Gral. Belgrano
    date(2026,  7,  9),   # Independencia
    date(2026,  8, 17),   # Paso a la Inmortalidad del Gral. San Martín
    date(2026, 10, 12),   # Día del Respeto a la Diversidad Cultural
    date(2026, 11, 23),   # Día de la Soberanía Nacional (trasladado del 20/vie al lunes 23)
    date(2026, 12,  8),   # Inmaculada Concepción de María
    date(2026, 12, 25),   # Navidad
})


def ruedas_entre(desde: date, hasta: date) -> int:
    """Ruedas BCBA transcurridas en (desde, hasta]: días hábiles no feriados posteriores
    a `desde` hasta `hasta` inclusive. 0 si hasta <= desde. El día de entrada no cuenta."""
    n, d = 0, desde
    while d < hasta:
        d += timedelta(days=1)
        if d.weekday() < 5 and d not in FERIADOS_BCBA:
            n += 1
    return n


def round_precio(price: float) -> float:
    """Redondea al tick mínimo BCBA según rango de precio."""
    if price >= 10000: tick = 5.0
    elif price >= 1000: tick = 1.0
    elif price >= 100:  tick = 0.5
    elif price >= 10:   tick = 0.05
    else:               tick = 0.01
    return round(round(price / tick) * tick, 2)
