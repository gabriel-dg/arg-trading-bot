"""Acumulador VWAP aproximado compartido por el bot y el importador histórico."""
import math

def update_vwap(state: dict, price: float, amount: float) -> bool:
    """Actualiza por delta de monto. True indica que hubo reinicio del contador.

    Al arrancar/reiniciar se aproxima todo el monto observado al precio actual;
    no pretende reconstruir operaciones anteriores. Si el contador retrocede
    se descarta el denominador previo: conservarlo mezclaría ruedas distintas.
    """
    if not math.isfinite(price) or price <= 0 or not math.isfinite(amount) or amount < 0:
        raise ValueError("Precio/monto inválido para VWAP")
    previous = state.get("last_monto")
    reset = previous is not None and amount < previous
    if previous is None or reset:
        state["cum_shares"] = amount / price
    elif amount > previous:
        state["cum_shares"] += (amount - previous) / price
    state["last_monto"] = amount
    return reset

def value(state: dict):
    shares = state.get("cum_shares", 0.0)
    amount = state.get("last_monto")
    return amount / shares if shares > 0 and amount is not None else None
