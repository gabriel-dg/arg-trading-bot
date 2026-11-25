"""Explicación determinista del audit; no genera señales ni usa LLM."""
from collections import Counter
import json


REASONS = {
    "entry_schedule": ("horario", "Fuera de una revisión de entrada habilitada; no significa ausencia de señal."),
    "outside_session": ("horario", "Fuera de una rueda habilitada por el calendario."),
    "no_session_snapshots": ("datos", "No hubo cortes observados para esta rueda."),
    "no_snapshot_quotes": ("datos", "El corte no tiene cotizaciones para evaluar entradas."),
    "missing_quote": ("datos", "Falta la cotización de un símbolo del universo."),
    "missing_position_quote": ("datos", "Falta una cotización fiable de una posición abierta; no se agrega riesgo."),
    "unreliable_data": ("datos", "La cotización no es fiable para esta decisión."),
    "classification_mismatch": ("datos", "La clasificación del instrumento no coincide con la referencia."),
    "outside_universe": ("universo", "El símbolo está fuera del universo habilitado."),
    "drawdown": ("señal", "La caída desde el máximo observable no alcanza el umbral de entrada."),
    "day_range": ("señal", "El precio está fuera de la zona admitida del rango diario."),
    "vwap": ("señal", "El precio supera el VWAP permitido para esta entrada."),
    "spread": ("liquidez", "La diferencia entre puntas supera el máximo permitido."),
    "drawdown_atr": ("señal", "La caída medida en ATR no alcanza el umbral."),
    "extended": ("señal", "El precio está demasiado extendido según rango/VWAP."),
    "event_extended": ("señal", "El precio está demasiado por encima del VWAP para la entrada por evento."),
    "held_or_pending": ("posición", "Ya hay una posición u orden pendiente para este símbolo."),
    "slots": ("límite", "Se alcanzó el máximo de posiciones, incluyendo compras pendientes."),
    "daily_loss": ("límite", "Se activó el límite de pérdida diaria; no se admiten nuevas compras."),
    "risk_or_capital": ("dimensionamiento", "El tamaño calculado es cero por fondos, topes, riesgo o redondeo a unidades; el audit no identifica una única causa."),
    "cooldown": ("espera", "Sigue vigente la espera para reingresar tras un cierre con pérdida."),
    "baseline": ("señal", "Señal mecánica de caída, rango y VWAP admitida por los filtros."),
    "ranked": ("señal", "Candidato admitido por la estrategia de ranking."),
    "event": ("señal", "Entrada admitida por un evento positivo disponible al decidir."),
    "catastrophic_stop": ("salida", "El precio cruzó el stop inicial y está habilitada la salida al primer toque."),
    "trailing_stop": ("salida", "Se alcanzaron las confirmaciones bajo el stop vigente, inicial o trailing según la fase."),
    "price_invalidation": ("salida", "El precio cruzó el nivel de invalidación de la tesis."),
    "event_invalidation": ("salida", "Un evento negativo disponible después de la apertura invalida la posición."),
    "timeout": ("ejecución", "Venció el tiempo de espera de la orden; una venta puede ser reemplazada."),
    "day_expiry": ("ejecución", "La orden pendiente venció al cambiar de rueda."),
    "exit_pending_buy": ("ejecución", "Se canceló el remanente de compra antes de intentar la salida."),
}

EVENTS = {
    "BUY_INTENT": "Intención de compra, aún no ejecutada",
    "SELL_INTENT": "Intención de venta, aún no ejecutada",
    "FILL": "Ejecución simulada (puede ser parcial)",
    "REJECT": "Candidato rechazado",
    "ENTRY_BLOCK": "Entradas bloqueadas",
    "ENTRY_SKIP": "Entrada no evaluada o ya ocupada",
    "MARKET_SKIP": "Corte fuera de rueda",
    "CANCEL": "Orden cancelada",
}


def decision_report(audit, orders, summary):
    by_id = {o["id"]: o for o in orders}
    timeline, counts = [], Counter()
    for index, row in enumerate(audit):
        order = by_id.get(row.get("order_id"), {})
        reason = row.get("reason", "")
        category, explanation = REASONS.get(reason, ("sin_clasificar", "Motivo sin traducción; consultar código y audit original."))
        item = {"audit_index": index, "ts": row["ts"], "symbol": row.get("symbol", order.get("symbol")),
                "event": row["event"], "reason": reason, "category": category,
                "action": EVENTS.get(row["event"], row["event"]), "explanation": explanation}
        for key in ("order_id", "side", "quantity", "price", "limit", "capital_context"):
            if key in row:
                item[key] = row[key]
        timeline.append(item)
        counts[(item["event"], reason, category)] += 1
    fills = [row for row in audit if row["event"] == "FILL"]
    buy_fills = sum(row.get("side") == "BUY" for row in fills)
    sell_fills = sum(row.get("side") == "SELL" for row in fills)
    return {
        "schema_version": 1, "kind": "paper_decision_explanation", "llm_calls": 0,
        "activity": {"buy_orders": sum(o["side"] == "BUY" for o in orders),
                     "sell_orders": sum(o["side"] == "SELL" for o in orders),
                     "buy_fill_events": buy_fills, "sell_fill_events": sell_fills,
                     "closed_trades": summary["closed_trades"],
                     "open_positions": len(summary["open_positions"]),
                     "orders_without_any_fill": sum(o["filled"] == 0 for o in orders),
                     "pending_orders": sum(o["status"] == "pending" for o in orders)},
        "headline": "Sin ejecuciones simuladas de compra." if not buy_fills else
                    f"{buy_fills} ejecuciones simuladas de compra y {sell_fills} de venta; pueden ser parciales.",
        "reason_counts": [{"event": event, "reason": reason, "category": category,
                           "count": count, "explanation": REASONS.get(reason, (None, "Motivo sin traducción."))[1]}
                          for (event, reason, category), count in sorted(counts.items())],
        "timeline": timeline,
        "limitations": ["Intención u orden no equivale a ejecución; un fill parcial no equivale a un trade cerrado.",
                        "Se explica el motivo registrado, no todos los filtros que habrían rechazado el candidato.",
                        "Los conteos son eventos de auditoría, no causas independientes ni porcentajes de oportunidades perdidas.",
                        "Sin universo/calendario de referencia no se detectan todos los símbolos o ruedas ausentes.",
                        "El informe no cambia parámetros ni demuestra rentabilidad o ejecución real."]}


def _cell(value):
    return str(value if value is not None else "—").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def write_decisions(out, audit, orders, summary):
    report = decision_report(audit, orders, summary)
    (out / "decisions.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    lines = ["# Decisiones explicadas", "", report["headline"], "",
             f"Órdenes de compra: {report['activity']['buy_orders']}; de venta: {report['activity']['sell_orders']}. "
             f"Trades cerrados: {report['activity']['closed_trades']}; posiciones abiertas: {report['activity']['open_positions']}.",
             "", "## Motivos registrados", "", "| Evento | Motivo | Cantidad | Explicación |", "|---|---|---:|---|"]
    for row in report["reason_counts"]:
        lines.append("| " + " | ".join(_cell(row[key]) for key in ("event", "reason", "count", "explanation")) + " |")
    lines += ["", "## Trazabilidad", "", "Cada fila apunta al índice (desde cero) del audit original. Horarios con zona horaria.",
              "", "| Audit | Fecha/hora | Símbolo | Acción | Explicación |", "|---:|---|---|---|---|"]
    for row in report["timeline"]:
        lines.append("| " + " | ".join(_cell(row[key]) for key in ("audit_index", "ts", "symbol", "action", "explanation")) + " |")
    lines += ["", "## Límites del informe", "", *[f"- {note}" for note in report["limitations"]], ""]
    (out / "decisions.md").write_text("\n".join(lines), encoding="utf-8")
