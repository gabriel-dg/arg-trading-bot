"""Pure entry and stop rules of the example strategy.

Everything here is deterministic and broker-free: it reads the daily indicators,
the day's VWAP and the current quotes, and returns decisions or stop levels. The
order path, persistence and risk bounds live in `argbot.strategy.candidate` and
`argbot.execution`.

Field and method names keep the original Spanish trading vocabulary so the JSON
configuration keys stay stable. Glossary:

- caida: drawdown of the last close from the max of the last `caida_lookback` days.
- rango del día: where the price sits in the day's [low, high] range, 0-100.
- régimen / breadth: share of the regime universe in a daily uptrend.
- ruedas: trading sessions.
- fase 1: the first sessions after an entry, where only the protective stop runs.
- lock ganador: part of a winner's best gain that the stop locks in.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time

# Candidate filters that rank the shortlist (best score first).
_MODOS_CON_RANKING = {"caida"}


@dataclass
class Decision:
    simbolo: str
    accion: str            # "BUY" | "SELL" | "HOLD"
    monto_ars: float = 0.0  # BUY only
    razon: str = ""


@dataclass
class StrategyConfig:
    """Parameters of the example strategy. Every value is configurable.

    The defaults are illustrative, not a recommendation. See
    `configs/examples/` for a full example configuration.
    """
    universe: list[str]
    # sizing
    max_capital_pct: float = 0.20      # max share of the day's capital per new position
    max_concurrent: int = 3            # max open positions at the same time
    sizing_overshoot_max: float = 1.0  # 1.0 = whole shares rounded down
    # candidate filter and entry rule
    filtro_candidatos: str = "caida"   # "caida" | "bull" | "ninguno"
    caida_lookback: int = 10           # sessions of the reference max
    caida_umbral: float = -0.08        # min (negative) drawdown to qualify
    mec_rango_dia_max: float = 0.50    # price in the lower part of the day's range
    mec_vwap_max: float = 0.0          # price <= VWAP * (1 + this)
    vwap_filtro_pct: float = 0.005     # skip candidates already extended over VWAP; <= 0 disables
    regimen_universe: list[str] | None = None  # breadth universe; None = whole universe
    regimen_umbral: float = 0.40       # breadth under this shrinks new positions...
    regimen_factor: float = 0.50       # ...by this factor (1.0 disables it)
    # stops
    trail_stop_pct: float = 0.03       # fixed trailing when there is no daily ATR
    trail_atr_mult: float = 2.5        # chandelier: max since entry - k * daily ATR
    stop_atr_mult: float = 2.0         # protective stop: entry - k * daily ATR
    trail_giveback_max: float = 0.08   # hard cap on what a winner gives back from its peak
    lock_ganador_umbral: float = 0.03  # a winner is one whose max passed entry * (1 + this)
    lock_ganador_frac: float = 0.5     # share of the best gain that is locked in
    stop_confirm_polls: int = 1        # consecutive polls under the stop before selling
    stop_fase1_ruedas: int = 0         # sessions with the protective stop only (0 = off)
    # orders and pacing
    compra_pasiva: bool = False        # True = limit BUY at the bid, never crossing the spread
    buy_timeout_min: int = 30
    sell_timeout_min: int = 10
    max_trades_dia: int = 15           # closes per day before entries stop
    cooldown_loss_min: int = 30        # pause per symbol after a losing close
    # session
    entry_windows: list[time] = field(default_factory=list)
    hora_no_entrada: time = time(16, 0)  # no BUY after this time


class RuleEngine:
    """Rule methods shared by the live candidate and the tests.

    Subclasses provide `config` (StrategyConfig), `_daily` (DailyIndicators or None),
    `_daily_sospechoso` (symbols whose daily series is not trusted), `_st` (positions,
    vwap, capital) and implement `_vwap(symbol)` and `_en_fase1_stop(pos, now)`.
    """

    config: StrategyConfig

    def _vwap(self, symbol: str) -> float | None:  # pragma: no cover - abstract
        raise NotImplementedError

    def _en_fase1_stop(self, pos: dict, now: datetime) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    def _breadth_diario(self, dia: date) -> tuple[int, int] | None:
        """(symbols in a daily uptrend, symbols with data). None without daily context."""
        if not self._daily:
            return None
        base = self.config.regimen_universe or self.config.universe
        evals = [s for s in base if self._daily.asof(s, dia)]
        if not evals:
            return None
        n_bull = sum(1 for s in evals
                     if (a := self._daily.asof(s, dia)) and a.ema_f > a.ema_s and a.slope > 0)
        return n_bull, len(evals)

    def _factor_regimen(self, dia: date) -> float:
        """Size multiplier for new positions from the daily breadth.

        1.0 with breadth >= regimen_umbral or without daily context; regimen_factor
        below it. A risk cap: it never decides what to buy, only how much.
        """
        b = self._breadth_diario(dia)
        if not b:
            return 1.0
        n_bull, n_tot = b
        return self.config.regimen_factor if n_bull / n_tot < self.config.regimen_umbral else 1.0

    @staticmethod
    def _rango_dia_pct(cot: dict, precio: float) -> float | None:
        """Position of the price in the day's [low, high] range, 0-100. None without a range."""
        hi = cot.get("maximo") or 0.0
        lo = cot.get("minimo") or 0.0
        if precio <= 0 or hi <= 0 or lo <= 0 or hi <= lo:
            return None
        return min(100.0, max(0.0, (precio - lo) / (hi - lo) * 100))

    def _trailing_stop_nivel(self, simbolo: str, pos: dict, max_p: float, now: datetime) -> float:
        """Stop level: the highest of the protective stop and the chandelier, never lowered.

        - Protective stop = entry - stop_atr_mult * daily ATR.
        - Chandelier = max since entry - trail_atr_mult * daily ATR; it rises with the
          price and persists overnight (max_p lives in the state).
        - Give-back cap: the stop is never further than trail_giveback_max from the peak.
        - Winner lock: once max >= entry * (1 + lock_ganador_umbral) the stop keeps
          lock_ganador_frac of the best gain.
        - Ratchet: the level is stored in pos["stop_nivel"] and never goes down, even
          if the daily ATR expands.
        During phase 1 only the protective stop runs. Without a daily ATR the stop
        falls back to a fixed percentage.
        """
        atr = None
        if self._daily and simbolo not in self._daily_sospechoso:
            # An ATR computed across an unadjusted split step is huge and would put the
            # stop below zero, so suspicious series use the percentage fallback.
            a = self._daily.asof(simbolo, now.date())
            atr = a.atr if a else None
        entrada = pos["precio_entrada"]
        fase1 = self._en_fase1_stop(pos, now)
        if atr and atr > 0:
            inicial = entrada - self.config.stop_atr_mult * atr
            if fase1:
                stop = inicial
            else:
                chandelier = max_p - self.config.trail_atr_mult * atr
                stop = max(chandelier, inicial)
                if self.config.trail_giveback_max:
                    stop = max(stop, max_p * (1 - self.config.trail_giveback_max))
        elif fase1:
            stop = entrada * (1 - self.config.trail_stop_pct)
        else:
            stop = max_p * (1 - self.config.trail_stop_pct)
        if not fase1 and self.config.lock_ganador_umbral and entrada > 0 and \
                max_p >= entrada * (1 + self.config.lock_ganador_umbral):
            stop = max(stop, entrada + self.config.lock_ganador_frac * (max_p - entrada))
        prev = pos.get("stop_nivel")
        if prev:
            stop = max(stop, prev)
        pos["stop_nivel"] = stop
        return stop

    def _simbolos_a_evaluar(self, cotizaciones: dict[str, dict], now: datetime,
                            modo: str | None = None) -> list[str]:
        """Shortlist: open positions first, then the qualifying candidates.

        Candidates without a position must pass `_score_estructural`, must not be
        extended over the day's VWAP, and are ranked by score when the filter ranks.
        Without daily context nothing is pre-filtered.
        """
        abiertas = {s for s, p in self._st["positions"].items() if p.get("en_posicion")}
        if not self._daily:
            return list(self.config.universe)
        scores: dict[str, float] = {}
        permite_entradas = (now.time() < self.config.hora_no_entrada
                            and not self._st.get("stop_diario_triggered"))
        # In the last entry window the VWAP "do not chase" filter does not apply.
        en_precierre = bool(self.config.entry_windows) and now.time() >= max(self.config.entry_windows)
        out: list[str] = []
        for s in self.config.universe:
            if s in abiertas:
                out.append(s)
                continue
            if not permite_entradas:
                continue
            if self._st["positions"].get(s, {}).get("buy_pendiente"):
                continue  # a BUY is already in flight
            cot = cotizaciones.get(s)
            if not cot:
                continue
            if s in self._daily_sospechoso:
                continue  # daily series inconsistent with the feed (unadjusted split?)
            score = self._score_estructural(s, now, modo)
            if score is None:
                continue
            if self.config.vwap_filtro_pct > 0 and not en_precierre:
                vwap = self._vwap(s)
                precio_s = cot.get("ultimoPrecio") or 0.0
                if vwap and precio_s > vwap * (1 + self.config.vwap_filtro_pct):
                    continue
            scores[s] = score
            out.append(s)
        if not _MODOS_CON_RANKING.intersection({modo or self.config.filtro_candidatos}):
            return out
        return [s for s in out if s in abiertas] + \
               sorted((s for s in out if s not in abiertas), key=lambda s: scores[s])

    def _score_estructural(self, simbolo: str, now: datetime,
                           modo: str | None = None) -> float | None:
        """Daily-structure score of a candidate. None = does not qualify. Lower is better."""
        modo = modo or self.config.filtro_candidatos
        if modo == "ninguno":
            return 0.0
        if modo == "bull":
            a = self._daily.asof(simbolo, now.date())
            if a and not (a.ema_f > a.ema_s and a.slope > 0):
                return None
            return 0.0
        if modo == "caida":
            c = self._daily.caida_desde_max(simbolo, now.date(), self.config.caida_lookback)
            if c is None:
                return 0.0
            return c if c <= self.config.caida_umbral else None
        raise ValueError(f"filtro_candidatos desconocido: {modo!r}")

    def _decisiones_mecanicas(self, cotizaciones: dict[str, dict], now: datetime,
                              simbolos: list[str] | None = None) -> list[Decision]:
        """Fixed entry rule: from the shortlist, in order, the candidates in an intraday
        pullback (day range <= mec_rango_dia_max and price <= VWAP * (1 + mec_vwap_max)),
        each sized at the maximum allowed, until the free slots are filled. Never SELL:
        exits belong to the stops. Without VWAP or a day range nothing is bought.
        """
        if simbolos is None:
            simbolos = self._simbolos_a_evaluar(cotizaciones, now)
        abiertas = {s for s, p in self._st["positions"].items() if p.get("en_posicion")}
        en_curso = {s for s, p in self._st["positions"].items() if p.get("buy_pendiente")}
        libres = self.config.max_concurrent - len(abiertas) - len(en_curso)
        if libres <= 0:
            return []
        total = self._st.get("capital_dia_total", 0.0) or self._st.get("capital_pool", 0.0)
        monto = self.config.max_capital_pct * self._factor_regimen(now.date()) * total
        out: list[Decision] = []
        for s in simbolos:
            if len(out) >= libres:
                break
            if s in abiertas or s in en_curso:
                continue
            cot = cotizaciones.get(s) or {}
            precio = cot.get("ultimoPrecio") or 0.0
            rango = self._rango_dia_pct(cot, precio)
            vwap = self._vwap(s)
            if precio <= 0 or rango is None or not vwap:
                continue
            if rango > self.config.mec_rango_dia_max * 100:
                continue
            if precio > vwap * (1 + self.config.mec_vwap_max):
                continue
            out.append(Decision(
                simbolo=s, accion="BUY", monto_ars=round(monto, 0),
                razon=f"regla mecánica: rango día {rango:.0f}% ≤ {self.config.mec_rango_dia_max*100:.0f}%, "
                      f"VWAPd {(precio / vwap - 1) * 100:+.2f}%",
            ))
        return out
