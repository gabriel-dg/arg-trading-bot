"""Cobertura e inactividad: conteos explícitos, sin relajar reglas ni inventar señales."""
from collections import Counter, defaultdict
from datetime import timedelta
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo
from argbot.lab.models import timestamp
from argbot.lab.reference import ReferenceBook, ZONE


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def write_report(output, report, lines):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    (output / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def coverage(capture, reference, *, start, end, interval_seconds, output):
    capture = Path(capture)
    book = ReferenceBook.load(reference)
    manifest = json.loads((capture / "manifest.json").read_text(encoding="utf-8"))
    for name, expected_hash in manifest["hashes"].items():
        if name not in {"quotes.jsonl", "requests.jsonl"}:
            raise ValueError("Archivo de manifiesto no permitido")
        if hashlib.sha256((capture / name).read_bytes()).hexdigest() != expected_hash:
            raise ValueError("La captura cambió desde su manifiesto")
    expected = {}
    stats = defaultdict(lambda: {"expected_slots": 0, "observed_rows": 0, "eligible_slots": 0,
                                  "issues": Counter(), "field_status": Counter()})
    for when, assets in book.slots(start, end, interval_seconds):
        day = when.astimezone(ZoneInfo(ZONE)).date().isoformat()
        for symbol in assets:
            expected[(when, symbol)] = (day, symbol)
            stats[(day, symbol)]["expected_slots"] += 1
    filled = set()
    unscheduled = 0
    rows = read_jsonl(capture / "quotes.jsonl") if "quotes.jsonl" in manifest["hashes"] else []
    for row in rows:
        requested = timestamp(row["requested_at"])
        if not start <= requested <= end:
            continue
        slot = start + timedelta(seconds=int((requested - start).total_seconds() // interval_seconds) * interval_seconds)
        key = (slot, row["symbol"])
        if key not in expected:
            unscheduled += 1
            continue
        stat = stats[expected[key]]
        stat["observed_rows"] += 1
        stat["issues"].update(row["issues"])
        stat["field_status"].update(f"{k}:{v}" for k, v in row["field_status"].items())
        if row["eligible"] and key not in filled:
            filled.add(key)
            stat["eligible_slots"] += 1
    result_rows = []
    for (day, symbol), stat in sorted(stats.items()):
        result_rows.append({"session": day, "symbol": symbol, **stat,
                            "missing_or_invalid_slots": stat["expected_slots"] - stat["eligible_slots"],
                            "coverage_pct": 100 * stat["eligible_slots"] / stat["expected_slots"]})
    attempts = read_jsonl(capture / "requests.jsonl") if "requests.jsonl" in manifest["hashes"] else []
    completed = [r for r in attempts if r.get("phase") == "completed"
                 and start <= timestamp(r["requested_at"]) <= end]
    report = {"kind": manifest["kind"], "start": start.isoformat(), "end": end.isoformat(),
              "interval_seconds": interval_seconds, "rows": result_rows,
              "requests": len(completed), "request_status": dict(Counter(str(r["status"]) for r in completed)),
              "unscheduled_rows": unscheduled,
              "reference_sha256": hashlib.sha256(Path(reference).read_bytes()).hexdigest(),
              "capture_manifest_sha256": hashlib.sha256((capture / "manifest.json").read_bytes()).hexdigest(),
              "limitations": ["Cobertura sobre grilla anclada en start, por instante de solicitud.",
                              "Elegible significa validación técnica; no demuestra exactitud del proveedor.",
                              "Calendario y universo conservan la procedencia declarada en sus versiones."]}
    lines = ["# Cobertura de captura", "", f"Tipo: {manifest['kind']}. Intervalo esperado: {interval_seconds} s.", "",
             "| Rueda | Símbolo | Esperados | Filas | Válidos únicos | Cobertura % |",
             "|---|---|---:|---:|---:|---:|"]
    for r in result_rows:
        lines.append(f"| {r['session']} | {r['symbol']} | {r['expected_slots']} | {r['observed_rows']} | {r['eligible_slots']} | {r['coverage_pct']:.1f} |")
    lines += ["", "## Calidad por símbolo y rueda", ""]
    for r in result_rows:
        lines.append(f"- {r['session']} / {r['symbol']}: {dict(r['issues'])}; ausentes o inválidos: {r['missing_or_invalid_slots']}.")
    lines += ["", *report["limitations"]]
    write_report(output, report, lines)
    return report


def inactivity(audit, orders, sessions, *, threshold=3):
    if type(threshold) is not int or threshold < 1:
        raise ValueError("El umbral debe ser un número positivo de ruedas")
    by_day = defaultdict(Counter)
    for row in audit:
        day = timestamp(row["ts"]).astimezone(ZoneInfo(ZONE)).date().isoformat()
        if row["event"] in {"REJECT", "ENTRY_BLOCK", "ENTRY_SKIP", "MARKET_SKIP"}:
            by_day[day][row["reason"]] += 1
        elif row["event"] == "BUY_INTENT":
            by_day[day]["buy_intents"] += 1
        elif row["event"] == "FILL" and row["side"] == "BUY":
            by_day[day]["buy_fills"] += 1
    for order in orders:
        if order["side"] == "BUY" and order["filled"] == 0:
            day = timestamp(str(order["submitted"])).astimezone(ZoneInfo(ZONE)).date().isoformat()
            by_day[day]["buy_pending_without_fill" if order["status"] == "pending" else "buy_terminal_without_fill"] += 1
    streak, alerts, rows = 0, [], []
    for day in sorted(set(sessions)):
        counters = by_day[day]
        streak = 0 if counters["buy_fills"] else streak + 1
        rows.append({"session": day, "counts": dict(counters), "sessions_without_buy_fill": streak})
        if streak >= threshold:
            alerts.append({"session": day, "sessions_without_buy_fill": streak,
                           "causes": dict(sum((by_day[r["session"]] for r in rows[-streak:]), Counter()))})
    return {"threshold_sessions": threshold, "definition": "Ruedas sin ningún fill de compra; parcial cuenta como entrada.",
            "sessions": rows, "alerts": alerts,
            "limitations": ["Conteos de decisiones, no porcentajes de causalidad independiente.",
                            "Un rechazo informa la primera regla que bloqueó; no todos los filtros contrafactuales.",
                            "El informe nunca modifica parámetros ni fuerza entradas."]}
