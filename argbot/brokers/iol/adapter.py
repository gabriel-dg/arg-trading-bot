"""Explicit IOL write boundary; never automatically replay a mutation.

Order/account readers are injected normalization functions returning OrderView /
AccountView. Production readers and migration must be verified before activation;
this module does not infer real fills or fees from an order acceptance or limit.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Callable

from argbot.brokers.iol.client import IOLClient
from argbot.execution import AccountView, BrokerRejected, OrderView

BROKER_MESSAGE_MAX = 200


def broker_message(rows) -> str | None:
    """The broker's rejection reasons, masked, for the order record.

    Runs of 6 or more digits (an account or document number) and e-mail addresses
    are covered; the result is capped.
    """
    parts = [row["description"].strip() for row in rows if isinstance(row, dict)
             and isinstance(row.get("description"), str) and row["description"].strip()]
    if not parts:
        return None
    text = " ".join("; ".join(parts).split())
    text = re.sub(r"\S+@\S+", "[correo]", text)
    text = re.sub(r"\d{6,}", "[numero]", text)
    return text[:BROKER_MESSAGE_MAX]


class NoReplayIOLClient(IOLClient):
    def _request(self, method, path, params=None, json_body=None):
        if method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            # Existing implementation refreshes/replays on 401 unless retried=True.
            # Pre-request auth still works. A mutation's uncertain outcome belongs
            # to the durable controller, not the HTTP client's retry loop.
            return self._do_request(method, path, params, json_body, retried=True)
        return super()._request(method, path, params, json_body)

    @staticmethod
    def _check_operar_response(data, op):
        # These are the explicit business rejection shapes supported by the
        # existing client. Do not classify arbitrary 2xx parsing errors as rejection.
        if isinstance(data, dict) and data.get("ok") is False and "numeroOperacion" in data:
            raise ValueError("Contradictory submission response")
        if isinstance(data, dict) and data.get("ok") is False and "numeroOperacion" not in data:
            rows = data.get("messages")
            raise BrokerRejected("explicit_business_rejection",
                                 detail=broker_message(rows if isinstance(rows, list) else []))
        if (isinstance(data, list) and data
                and all(isinstance(row, dict) and "description" in row for row in data)):
            raise BrokerRejected("explicit_business_rejection", detail=broker_message(data))
        return data


class IOLExecutionBroker:
    """Wrap an explicitly supplied client; no credentials or defaults loaded here.

    Callbacks MUST provide complete confirmed fills/fees, holdings, available
    cash and the complete active-order set for the same account. A missing field
    must raise, not become a guessed price, zero fee or empty position list.
    """

    def __init__(self, client: NoReplayIOLClient, *,
                 read_order: Callable[[NoReplayIOLClient, str], OrderView],
                 read_account: Callable[[NoReplayIOLClient], AccountView],
                 market="bCBA", settlement="t1"):
        if not isinstance(client, NoReplayIOLClient):
            raise TypeError("Mutations require NoReplayIOLClient")
        if not callable(read_order) or not callable(read_account):
            raise TypeError("Explicit verified broker readers are required")
        self.client = client
        self.read_order, self.read_account = read_order, read_account
        self.market, self.settlement = market, settlement

    def submit(self, intent):
        method = self.client.comprar if intent["side"] == "BUY" else self.client.vender
        payload = method(mercado=self.market, simbolo=intent["symbol"],
                         cantidad=intent["quantity"], precio=float(intent["limit"]),
                         plazo=self.settlement,
                         validez=datetime.fromisoformat(intent["created_at"]).date())
        if not isinstance(payload, dict):
            raise ValueError("Unrecognized submission response")
        number = payload.get("numeroOperacion")
        if (isinstance(number, bool) or not isinstance(number, (str, int))
                or not str(number).isdigit() or int(number) <= 0):
            raise ValueError("Submission response has no valid operation number")
        return str(number)

    def order(self, broker_id):
        return self.read_order(self.client, broker_id)

    def account(self):
        return self.read_account(self.client)

    def cancel(self, broker_id):
        self.client.cancelar_orden(int(broker_id))
