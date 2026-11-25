"""Ejecución reproducible con manifiesto de datos, configuración y código."""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from argbot.lab.data import digest, load_quotes
from argbot.lab.engine import Engine
from argbot.lab.events import EventBook
from argbot.lab.provenance import object_hash, provenance

def code_digest():
    root = Path(__file__).resolve().parent
    h = hashlib.sha256()
    for p in sorted(root.glob("*.py")) + [root.parent / "exchange.py", root.parent / "strategy" / "daily.py", root.parent / "strategy" / "vwap.py"]:
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()

def run(path, config, out, *, capital=200000.0, event_path=None, reference_path=None, inactivity_sessions=3, limits=None):
    from argbot.lab.settings import BotSettings
    settings = limits if isinstance(limits, BotSettings) else None
    if settings is not None and settings.events_enabled and event_path is None:
        raise ValueError("Settings con eventos requieren un conjunto explícito de eventos")
    if settings is not None:
        config, limits = settings.apply(config), settings.limits
    out = Path(out)
    if out.exists() and any(out.iterdir()):
        raise ValueError(f"{out} no está vacío: cada corrida necesita un destino propio")
    frames = load_quotes(Path(path))
    metadata_path = Path(path).with_suffix(".manifest.json")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {"kind": "unspecified"}
    if metadata.get("csv_sha256") and metadata["csv_sha256"] != digest(path):
        raise ValueError("El hash del CSV no coincide con su manifiesto")
    book = EventBook.load(Path(event_path)) if event_path else EventBook()
    from argbot.lab.reference import ReferenceBook
    from argbot.lab.observability import inactivity
    from datetime import datetime, time, timedelta
    reference = ReferenceBook.load(reference_path) if reference_path else None
    if limits is None:
        engine = Engine(config, capital, book, reference=reference)
    else:
        from argbot.lab.settings_engine import SettingsEngine
        engine = SettingsEngine(config, capital, book, reference=reference, limits=limits,
                                schedule=settings.schedule if settings else None)
    for now, quotes in frames:
        engine.step(now, quotes)
    if not engine.curve:
        raise ValueError("Sin observaciones dentro de la rueda")
    out.mkdir(parents=True, exist_ok=True)
    source_info = provenance()
    manifest = {"config": asdict(config), "dataset": metadata, "csv_sha256": digest(path),
                "events_sha256": digest(event_path) if event_path else None,
                "code_sha256": code_digest(), "git_revision": source_info["git_revision"],
                "git_dirty": source_info["git_dirty"],
                "period": [frames[0][0].isoformat(), frames[-1][0].isoformat()],
                "capital_initial": capital, "execution": "next_snapshot_limit_orders",
                "performance_evidence": False,
                "reference_sha256": digest(reference_path) if reference_path else None,
                "inactivity_sessions": inactivity_sessions}
    manifest["provenance"] = source_info
    manifest["config_sha256"] = object_hash(asdict(config))
    if limits is not None:
        snapshot = (settings or limits).snapshot()
        manifest["operating_settings"] = snapshot
        manifest["operating_settings_sha256"] = object_hash(snapshot)
        manifest["settings_precedence"] = "operating_settings override corresponding rules; config records applied exits/execution; historical contracts unchanged"
        (out / "settings.json").write_text(json.dumps(snapshot, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    manifest["dataset_manifest_sha256"] = digest(metadata_path) if metadata_path.exists() else None
    summary = engine.summary()
    summary["dataset_kind"] = metadata["kind"]
    sessions = sorted({r["ts"][:10] for r in engine.curve})
    if reference:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo("America/Argentina/Buenos_Aires")
        day, end = frames[0][0].astimezone(zone).date(), frames[-1][0].astimezone(zone).date()
        sessions = []
        while day <= end:
            asof = datetime.combine(day, time(23, 59), tzinfo=zone)
            asof = min(asof, frames[-1][0])
            if reference.hours(day, asof):
                sessions.append(day.isoformat())
                if not any(r["ts"][:10] == day.isoformat() for r in engine.curve):
                    engine.record(asof, "ENTRY_BLOCK", reason="no_session_snapshots")
            day += timedelta(days=1)
    activity = inactivity(engine.audit, engine.broker.records(), sessions, threshold=inactivity_sessions)
    activity["session_basis"] = "versioned_calendar" if reference else "observed_sessions_only"
    if not reference:
        activity["limitations"].append("Sin --reference no se detectan ruedas enteras ni símbolos ausentes del dataset.")
    for name, obj in (("manifest.json", manifest), ("summary.json", summary),
                      ("orders.json", engine.broker.records()), ("trades.json", engine.trades),
                      ("equity.json", engine.curve), ("inactivity.json", activity)):
        (out / name).write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str, allow_nan=False), encoding="utf-8")
    (out / "audit.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in engine.audit), encoding="utf-8")
    lines = ["# Inactividad", "", activity["definition"], "",
             "| Rueda | Ruedas sin fill de compra | Motivos y actividad |", "|---|---:|---|"]
    for row in activity["sessions"]:
        lines.append(f"| {row['session']} | {row['sessions_without_buy_fill']} | {row['counts']} |")
    lines += ["", f"Alertas: {len(activity['alerts'])}; umbral: {inactivity_sessions} ruedas.", "", *activity["limitations"]]
    (out / "inactivity.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if limits is not None:
        from argbot.lab.decision_report import write_decisions
        write_decisions(out, engine.audit, engine.broker.records(), summary)
    return summary

def compare(path, configs, out, capital=200000.0, event_path=None, *, reference_path=None, inactivity_sessions=3):
    out = Path(out)
    if out.exists() and any(out.iterdir()):
        raise ValueError("El directorio de comparación ya contiene resultados")
    names = [c.name for c in configs]
    if len(set(names)) != len(names) or any(not n.replace("_", "").replace("-", "").isalnum() for n in names):
        raise ValueError("Nombres de perfiles duplicados o inválidos")
    summaries = [run(path, cfg, out / cfg.name, capital=capital, event_path=event_path,
                     reference_path=reference_path, inactivity_sessions=inactivity_sessions) for cfg in configs]
    lines = ["# Comparación de simulaciones", "",
             "Estos números describen el modelo de ejecución y los datos elegidos. No son resultados en vivo.", "",
             "| Perfil | Retorno % | Caída máxima % | Cierres | Aciertos % | Exposición media % |",
             "|---|---:|---:|---:|---:|---:|"]
    for r in summaries:
        wr = f'{r["win_rate_pct"]:.1f}' if r["win_rate_pct"] is not None else "—"
        lines.append(f'| {r["profile"]} | {r["return_pct"]:.2f} | {r["max_drawdown_pct"]:.2f} | {r["closed_trades"]} | {wr} | {r["average_exposure_pct"]:.1f} |')
    lines += ["", "Cada subdirectorio conserva configuración, hashes, órdenes, fills, rechazos y curva.",
              "No se elige automáticamente un ganador ni se cambian parámetros a partir de esta tabla.", ""]
    (out / "comparison.md").write_text("\n".join(lines), encoding="utf-8")
    return summaries
