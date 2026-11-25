"""IOL market data: quotes and unadjusted daily history through the IOL client."""
from datetime import timedelta

from argbot.market import daily_rows


class IOLMarket:
    def __init__(self, client, budget=None):
        self.client = client
        self.budget = budget

    def set_priority(self, symbols):
        if self.budget is not None:
            self.budget.priority = set(symbols)

    def quote(self, symbol):
        return self.client.get_cotizacion('bCBA', symbol, 't1')

    def history(self, symbols, session_date, previous_session):
        result, excluded = {}, {}
        self.history_diagnostics={}
        for symbol in symbols:
            try:
                raw = self.client.get_historico('bCBA', symbol, session_date-timedelta(days=150),
                                               session_date, ajustada='sinAjustar')
                self.history_diagnostics[symbol]={}
                result[symbol] = daily_rows(raw, session_date, previous_session,audit=self.history_diagnostics[symbol])
            except Exception as exc:
                allowed={'insufficient_or_stale_history','conflicting_daily_summary'}
                excluded[symbol] = str(exc) if type(exc) is ValueError and str(exc) in allowed else type(exc).__name__
        return result, excluded
