"""Read-only terminal dashboard for open positions. No broker or LLM calls.

Reads the local state recorded by the bot (`execution-state.json`): free cash,
open positions, the last quote the bot recorded for each symbol, and the day
valuation. Prices are as recent as the bot's last completed round. Never writes
files, never acquires the account lease, and never queries any broker.

Usage: argbot watch-positions --state-dir <dir> [--config <cfg>] [--once] [--interval 10]
"""
from __future__ import annotations

import argparse
from datetime import datetime
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import sys
import time as timing
from zoneinfo import ZoneInfo

from argbot.execution import TERMINAL, instant
from argbot.paper_session import load_config

ART = ZoneInfo("America/Argentina/Buenos_Aires")
GREEN, RED, YELLOW, BOLD, RESET = "\x1b[32m", "\x1b[31m", "\x1b[33m", "\x1b[1m", "\x1b[0m"


def read_json(path: Path | str, *, attempts: int = 5, pause: float = 0.2, sleep=timing.sleep) -> dict:
    """The bot replaces its state atomically; Windows may deny a read meanwhile."""
    for attempt in range(attempts):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (PermissionError, json.JSONDecodeError):
            if attempt == attempts - 1:
                raise
            sleep(pause)


def money(value) -> Decimal:
    return Decimal(str(value))


def fee_estimates(state: dict, reserve) -> dict[str, dict[str, str]]:
    """Confirmed historic charges are indicative, not a promised future tariff."""
    result = {}
    for side in ("BUY", "SELL"):
        orders = [
            o for o in state["orders"].values()
            if o["side"] == side and o["state"] in TERMINAL and o["filled"] and money(o["gross"]) > 0
        ]
        gross = sum((money(o["gross"]) for o in orders), Decimal(0))
        rate = sum((money(o["fees"]) for o in orders), Decimal(0)) / gross if gross else money(reserve)
        if not 0 <= rate < 1:
            raise ValueError("Invalid observed fee rate")
        result[side] = {
            "rate": str(rate),
            "basis": "confirmed_historical_charges" if gross else "configured_reserve_estimate",
        }
    return result


def ars(value, *, sign: bool = False) -> str:
    """Argentine format: $1.234 or -$1.234 (whole pesos)."""
    amount = int(money(value).quantize(Decimal("1")))
    text = f"{abs(amount):,}".replace(",", ".")
    prefix = "-" if amount < 0 else "+" if sign and amount > 0 else ""
    return f"{prefix}${text}"


def price(value) -> str:
    """Prices keep their decimals when they have them: 1.234 or 4.567,5."""
    number = money(value)
    whole = int(number)
    cents = number - whole
    text = f"{whole:,}".replace(",", ".")
    return text if cents == 0 else text + "," + f"{cents:.2f}"[2:].rstrip("0")


def fixed(value, decimals: int = 1) -> str:
    """Same decimals on every row, so a column lines up: 1.234,0 and 5.678,9."""
    text = f"{money(value):,.{decimals}f}"
    return text.replace(",", "_").replace(".", ",").replace("_", ".")


def pct(value) -> str:
    return f"{value * 100:+.2f}%".replace(".", ",")


def age(at: str | None, now: datetime) -> str:
    """Short at any age, so it fits its column: 45 s, 12 min, 17 h 13, 2 d."""
    if not at:
        return "s/d"
    seconds = max(0, int((now - instant(at)).total_seconds()))
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400:
        return f"{seconds // 3600} h {seconds % 3600 // 60:02d}"
    return f"{seconds // 86400} d"


def collect(state_dir: Path | str, config_path: Path | str, *, now: datetime | None = None) -> dict:
    """Everything the screen shows, from local files only."""
    now = now or datetime.now(ART)
    cfg = load_config(config_path)
    envelope = read_json(Path(state_dir) / "execution-state.json")
    state = envelope["state"]
    runtime = state.get("strategy") or {}
    quotes = runtime.get("quotes") or {}
    # The sale commission actually charged so far (falls back to the configured reserve).
    sell_rate = money(fee_estimates(state, cfg["fee_reserve"])["SELL"]["rate"])
    lock = (cfg.get("strategy") or {}).get("lock_ganador_umbral") or 0
    rows = []
    for symbol, position in sorted(state["positions"].items()):
        quote = quotes.get(symbol) or {}
        meta = position.get("swing") or {}
        quantity, cost = position["quantity"], money(position["cost"])
        bid = quote.get("bid")
        value = quantity * money(bid) * (1 - sell_rate) if bid is not None else None
        entry = meta.get("precio_entrada") or (cost / quantity)
        peak = meta.get("max_precio_desde_entrada") or entry
        last = quote.get("ultimoPrecio")
        # Price move only: purchase price against the last traded price, no commissions.
        gross = None if last is None else quantity * (money(last) - money(entry))
        rows.append(dict(
            symbol=symbol,
            quantity=quantity,
            entry=entry,
            last=last,
            gross=gross,
            gross_pct=None if last is None else money(last) / money(entry) - 1,
            quote_at=quote.get("source_at"),
            cost=cost,
            value=value,
            result=None if value is None else value - cost,
            result_pct=None if value is None else (value - cost) / cost,
            stop=meta.get("stop_nivel"),
            lock=bool(lock) and money(peak) >= money(entry) * (1 + money(lock)),
        ))
    holds = sum((money(o["hold"]) for o in state["orders"].values()), Decimal(0))
    invested = sum((r["value"] for r in rows if r["value"] is not None), Decimal(0))
    equity = money(runtime["equity"]) if runtime.get("equity") is not None else None
    opening = money(runtime["opening_equity"]) if runtime.get("opening_equity") is not None else None
    return dict(
        now=now,
        session=runtime.get("day") or "",
        status=runtime.get("status"),
        heartbeat_at=runtime.get("heartbeat_at"),
        entries_blocked=runtime.get("entries_blocked"),
        risk_paused=bool(runtime.get("risk_paused")),
        cash=money(state["cash"]),
        holds=holds,
        invested=invested,
        equity=equity,
        valuation_at=runtime.get("valuation_at"),
        opening=opening,
        brake=money(cfg["max_daily_loss_pct"]),
        sell_rate=sell_rate,
        rows=rows,
        missing=[r["symbol"] for r in rows if r["value"] is None],
    )


def colored(text: str, value, color: bool) -> str:
    """Color after padding, so escape codes never shift the columns."""
    if not color or value is None or value == 0:
        return text
    return (GREEN if value > 0 else RED) + text + RESET


ANSI = re.compile(r"\x1b\[[0-9;]*m")
CYAN, DIM = "\x1b[36m", "\x1b[2m"
UNICODE_BOX = dict(
    h="─", v="│", tl="╭", tr="╮", bl="╰", br="╯", ml="├", mr="┤",
    ttl="┌", ttr="┐", tbl="└", tbr="┘", tm="┬", bm="┴", cross="┼", dot="●",
)
ASCII_BOX = dict(
    h="-", v="|", tl="+", tr="+", bl="+", br="+", ml="+", mr="+",
    ttl="+", ttr="+", tbl="+", tbr="+", tm="+", bm="+", cross="+", dot="*",
)
# (title, width, alignment) of the positions table; the width excludes the 1-space padding.
COLUMNS = (
    ("Papel", 6, "<"),
    ("Cant", 5, ">"),
    ("Precio compra", 13, ">"),
    ("Precio actual", 13, ">"),
    ("Hace", 7, ">"),
    ("Resultado", 18, ">"),
    ("Resultado neto", 18, ">"),
    ("Stop", 14, ">"),
)


def visible(text: str) -> int:
    return len(ANSI.sub("", text))


def pad(text: str, width: int, align: str = "<") -> str:
    """Pad by VISIBLE width, so color codes never shift a column. A longer text is cut
    (plain, with an ellipsis): one wide cell would push every border to its right."""
    if visible(text) > width:
        text = ANSI.sub("", text)[:max(0, width - 1)] + "…"
    space = " " * max(0, width - visible(text))
    return text + space if align == "<" else space + text


def paint(text: str, code: str, color: bool) -> str:
    return code + text + RESET if color and code else text


def tone(value) -> str | None:
    return None if value is None or value == 0 else (GREEN if value > 0 else RED)


def render(view: dict, *, color: bool = True, box: dict = UNICODE_BOX) -> str:
    b, now = box, view["now"]
    width = sum(w + 3 for _, w, _ in COLUMNS) + 1  # every cell is "│ text "; then the closing │
    inner = width - 4

    def framed(text: str) -> str:
        return f"{b['v']} {pad(text, inner)} {b['v']}"

    beat = view["heartbeat_at"]
    quiet = (now - instant(beat)).total_seconds() if beat else None
    if not view["session"]:
        bot = paint(f"{b['dot']} sin sesión iniciada", YELLOW, color)
        stamp = f"sin rueda · {now:%H:%M:%S}"
    else:
        if view["session"] != now.date().isoformat():
            last = f"{view['session'][8:10]}/{view['session'][5:7]}"
            bot = paint(f"{b['dot']} esperando el arranque de hoy · datos de la rueda del {last}", YELLOW, color)
        elif quiet is None or quiet > 180:
            bot = paint(f"{b['dot']} SIN SEÑALES " + (f"hace {age(beat, now)}" if beat else "(sin latido)"), RED + BOLD, color)
        else:
            names = {"running": "operando", "blocked": "operando con restricciones", "stopped": "detenido"}
            state = names.get(view["status"], view["status"] or "s/d")
            bot = paint(b["dot"], GREEN, color) + f" {state} · última señal hace {age(beat, now)}"
        stamp = f"rueda {view['session'][8:10]}/{view['session'][5:7]} · {now:%H:%M:%S}"
    title = paint("ARGBOT · POSICIONES ABIERTAS", BOLD + CYAN, color)
    head = title + " " * max(1, inner - visible(title) - visible(stamp)) + stamp
    lines = [b["tl"] + b["h"] * (width - 2) + b["tr"], framed(head), framed(f"Bot: {bot}")]
    if view["entries_blocked"] or view["risk_paused"]:
        note = "Freno diario activo: no compra" if view["risk_paused"] else "Compras en pausa: " + view["entries_blocked"]
        lines.append(framed(paint(note, YELLOW, color)))
    lines.append(b["ml"] + b["h"] * (width - 2) + b["mr"])
    total = view["cash"] + view["holds"] + view["invested"]
    money_line = (
        f"Disponible {paint(ars(view['cash']), BOLD, color)}    Invertido {ars(view['invested'])}"
        f"    Total {paint(ars(total), BOLD, color)}"
    )
    if view["holds"]:
        money_line += f"    Reservado en órdenes {ars(view['holds'])}"
    lines.append(framed(money_line))
    if view["equity"] is not None and view["opening"]:
        day = view["equity"] - view["opening"]
        rate = day / view["opening"]
        margin = rate + view["brake"]
        lines.append(framed(
            "Resultado del día "
            + paint(f"{ars(day, sign=True)} ({pct(rate)})", tone(day), color)
            + f"    apertura {ars(view['opening'])}    freno -{view['brake'] * 100:.0f}%"
            + f" (faltan {pct(margin)[1:]})    valuación de hace {age(view['valuation_at'], now)}"
        ))
    lines.append(b["bl"] + b["h"] * (width - 2) + b["br"])
    if not view["rows"]:
        lines.append("  Sin posiciones abiertas.")
        return "\n".join(lines)

    def rule(left: str, mid: str, right: str) -> str:
        return left + mid.join(b["h"] * (w + 2) for _, w, _ in COLUMNS) + right

    def row(cells) -> str:
        return b["v"] + b["v"].join(f" {pad(c, w, a)} " for c, (_, w, a) in zip(cells, COLUMNS)) + b["v"]

    def amount(value, rate) -> str:
        if value is None:
            return "sin precio"
        return paint(f"{ars(value, sign=True):>9s} {pct(rate):>8s}", tone(value), color)

    lines += [
        rule(b["ttl"], b["tm"], b["ttr"]),
        row([paint(t, BOLD, color) for t, _, _ in COLUMNS]),
        rule(b["ml"], b["cross"], b["mr"]),
    ]
    for r in view["rows"]:
        # Price and its age in separate columns: an age of "54 s" or "12 min" never shifts the price.
        stop = "s/d" if r["stop"] is None else fixed(r["stop"])
        if r["lock"]:
            stop = paint("lock", YELLOW, color) + " " + stop
        lines.append(row([
            paint(r["symbol"], BOLD, color),
            str(r["quantity"]),
            price(r["entry"]),
            price(r["last"]) if r["last"] is not None else "s/d",
            paint(age(r["quote_at"], now), DIM, color) if r["last"] is not None else "",
            amount(r["gross"], r["gross_pct"]),
            amount(r["result"], r["result_pct"]),
            stop,
        ]))
    priced = [r for r in view["rows"] if r["gross"] is not None]
    paid = sum((money(r["entry"]) * r["quantity"] for r in priced), Decimal(0))
    gross = sum((r["gross"] for r in priced), Decimal(0))
    net = sum((r["result"] for r in priced if r["result"] is not None), Decimal(0))
    cost = sum((r["cost"] for r in priced if r["result"] is not None), Decimal(0))
    lines += [
        rule(b["ml"], b["cross"], b["mr"]),
        row([
            paint("TOTAL", BOLD, color),
            "",
            "",
            "",
            "",
            amount(gross, gross / paid if paid else 0),
            amount(net, net / cost if cost else 0),
            "",
        ]),
        rule(b["tbl"], b["bm"], b["tbr"]),
        paint("  Resultado: precio actual contra precio de compra, sin comisiones.", DIM, color),
        paint(
            f"  Resultado neto: al precio de compra vigente, con la comisión de compra pagada y la de venta "
            f"cobrada hasta hoy ({pct(view['sell_rate'])[1:]}).",
            DIM,
            color,
        ),
        paint("  Precios de la última ronda del bot. Solo lectura: no consulta al broker ni cambia nada.", DIM, color),
    ]
    return "\n".join(lines)


def supports(box: dict, stream) -> bool:
    try:
        "".join(box.values()).encode(stream.encoding or "ascii")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def watch(state_dir: Path | str, config_path: Path | str, *, once: bool = False, interval: int = 10) -> None:
    if os.name == "nt":
        os.system("")  # enable ANSI colors in the Windows console
    interval = max(5, interval)
    color = sys.stdout.isatty()
    box = UNICODE_BOX if supports(UNICODE_BOX, sys.stdout) else ASCII_BOX
    while True:
        try:
            screen = render(collect(state_dir, config_path), color=color, box=box)
        except Exception as exc:  # never crash the viewer; the bot is independent
            screen = f"No se pudo leer el estado del bot ({type(exc).__name__}). Reintento en {interval} s."
        if once:
            print(screen)
            return
        print("\x1b[2J\x1b[H" + screen + f"\n\n  Actualiza cada {interval} s · Ctrl+C para salir", flush=True)
        try:
            timing.sleep(interval)
        except KeyboardInterrupt:
            return


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--state-dir", type=Path, required=True, help="Folder containing execution-state.json")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "configs/examples/strategy.example.json",
    )
    parser.add_argument("--once", action="store_true", help="Mostrar una vez y salir")
    parser.add_argument("--interval", type=int, default=10, help="Segundos entre actualizaciones (minimo 5)")
    args = parser.parse_args(argv)
    watch(args.state_dir, args.config, once=args.once, interval=args.interval)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        pass
