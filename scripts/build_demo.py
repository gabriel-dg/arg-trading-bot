"""Genera precios y eventos FICTICIOS deterministas para aprender y probar el motor."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
from argbot.lab.data import write_quotes, digest
from argbot.lab.models import Quote
from argbot.exchange import FERIADOS_BCBA

def main():
    root = Path(__file__).resolve().parents[1]
    path = root / "examples" / "synthetic.csv"
    day = datetime(2026, 9, 7, 10, 30, tzinfo=timezone(timedelta(hours=-3)))
    rows, days = [], []
    while len(days) < 30:
        if day.weekday() < 5 and day.date() not in FERIADOS_BCBA:
            days.append(day)
        day += timedelta(days=1)
    for index, day in enumerate(days):
        for k in range(27):  # cada 15 minutos, hasta 17:00
            now = day + timedelta(minutes=15*k)
            for j, symbol in enumerate(("DEMO_A", "DEMO_B", "DEMO_C")):
                base = 100 + 12*math.sin((index+j*2)/3)
                price = base + 1.5*math.sin(k/4+j)
                # Indicadores fabricados a propósito; no son datos de una acción real.
                rows.append(Quote(now, symbol, round(price, 4), round(price-0.05,4),
                                  round(price+0.05,4), base-1.6, base+1.6,
                                  base+0.4, 2.0, 114.0, base-3.0,
                                  base >= 100, ("banks", "energy", "cedear")[j],
                                  bid_size=50, ask_size=50))
    write_quotes(path, rows)
    path.with_suffix(".manifest.json").write_text(json.dumps({
        "kind": "synthetic", "generator": "scripts/build_demo.py", "csv_sha256": digest(path),
        "currency": "ARS", "price_basis": "raw_no_actions",
        "description": "Empresas, precios y eventos ficticios. Solo demuestra funcionamiento; NO mide edge."},
        indent=2, ensure_ascii=False), encoding="utf-8")
    doc = "DEMO_C anunció un contrato ficticio que aumenta su capacidad prevista. Ejemplo educativo."
    event = {
        "id": "synthetic-contract", "symbol": "DEMO_C", "source_url": "https://example.org/demo-c",
        "published_at": days[1].isoformat(), "observed_at": (days[1]+timedelta(minutes=1)).isoformat(),
        "analyzed_at": (days[1]+timedelta(minutes=2)).isoformat(),
        "expires_at": (days[1]+timedelta(days=2)).isoformat(),
        "fact": "Contrato ficticio anunciado", "interpretation": "Hipótesis de aumento de ingresos",
        "direction": "positive", "invalidation": "Cancelación del contrato ficticio",
        "evidence": doc, "source_hash": hashlib.sha256(doc.encode()).hexdigest(),
        "model": "synthetic-fixture"}
    (path.parent/"synthetic_events.jsonl").write_text(json.dumps(event, ensure_ascii=False)+"\n", encoding="utf-8")
    print(f"Generadas {len(rows)} cotizaciones ficticias en {path}")

if __name__ == "__main__":
    main()
