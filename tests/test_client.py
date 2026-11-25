"""IOLClient contra respuestas IOL mockeadas (httpx.MockTransport).

Las shapes replican las observadas empíricamente:
éxito 201 {"numeroOperacion"}, error Comprar 202 {ok, messages},
error Vender 202 lista plana, historico vacío → IOLNoDataError.
"""

import json

import httpx
import pytest

from argbot.brokers.iol.client import IOLClient, _normalize_pais
from argbot.brokers.iol.exceptions import IOLAPIError, IOLAuthError, IOLNoDataError


class FakeAuth:
    base_url = "https://api.test"

    def __init__(self):
        self.invalidations = 0

    def headers(self):
        return {"Authorization": "Bearer tok", "Accept": "application/json"}

    def invalidate_access_token(self):
        self.invalidations += 1

    def close(self):
        pass


def make_client(handler) -> tuple[IOLClient, FakeAuth]:
    auth = FakeAuth()
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return IOLClient(auth=auth, http_client=http), auth


def _json(status: int, body) -> httpx.Response:
    return httpx.Response(status, json=body)


# ── flujo básico y errores HTTP ───────────────────────────────────────────────

def test_get_ok():
    client, _ = make_client(lambda req: _json(200, {"cuentas": [{"tipo": "pesos"}]}))
    data = client.get_estado_cuenta()
    assert data["cuentas"][0]["tipo"] == "pesos"


def test_401_reintenta_una_vez_e_invalida_token():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return _json(401, {}) if calls["n"] == 1 else _json(200, {"ok": True})

    client, auth = make_client(handler)
    data = client.get_estado_cuenta()
    assert data == {"ok": True}
    assert calls["n"] == 2
    assert auth.invalidations == 1


def test_401_persistente_es_autherror():
    client, auth = make_client(lambda req: _json(401, {}))
    with pytest.raises(IOLAuthError) as caught:
        client.get_estado_cuenta()
    # A refusal, never retried at 30 s.
    assert type(caught.value).__name__ == "IOLAuthRejectedError"
    assert auth.invalidations == 1, "solo un retry"


def test_500_es_apierror_con_status():
    client, _ = make_client(lambda req: _json(500, {"error": "interno"}))
    with pytest.raises(IOLAPIError) as exc:
        client.get_estado_cuenta()
    assert exc.value.status_code == 500


def test_2xx_no_json_es_apierror():
    client, _ = make_client(lambda req: httpx.Response(200, text="<html>mantenimiento</html>"))
    with pytest.raises(IOLAPIError):
        client.get_estado_cuenta()


def test_error_de_red_es_apierror_status_0():
    def handler(req):
        raise httpx.ConnectError("conexión rechazada")

    client, _ = make_client(handler)
    with pytest.raises(IOLAPIError) as exc:
        client.get_estado_cuenta()
    assert exc.value.status_code == 0, "status 0 = resultado desconocido para operar"


# ── Comprar / Vender response shapes (synthetic values) ───────────────────────

def test_comprar_exito_201():
    from datetime import date
    client, _ = make_client(lambda req: _json(201, {"numeroOperacion": 100000001}))
    resp = client.comprar("bCBA", "QA0", 10, 100.0, "t1", date.today())
    assert resp["numeroOperacion"] == 100000001


def test_comprar_error_negocio_202_dict():
    from datetime import date
    body = {"ok": False, "messages": [{"title": "", "description": "Cantidad mínima 100"}]}
    client, _ = make_client(lambda req: _json(202, body))
    with pytest.raises(IOLAPIError, match="Cantidad mínima 100"):
        client.comprar("bCBA", "QA1", 10, 50.0, "t1", date.today())


def test_vender_error_negocio_202_lista_plana():
    from datetime import date
    body = [{"title": "", "description": "El título no se encuentra en el portafolio."}]
    client, _ = make_client(lambda req: _json(202, body))
    with pytest.raises(IOLAPIError, match="no se encuentra en el portafolio"):
        client.vender("bCBA", "QA0", 10, 100.0, "t1", date.today())


def test_comprar_body_completo():
    from datetime import datetime
    captured = {}

    def handler(req):
        captured.update(json.loads(req.content))
        return _json(201, {"numeroOperacion": 1})

    client, _ = make_client(handler)
    client.comprar("bCBA", "QA0", 10, 100.5, "t1", datetime(2026, 6, 11, 16, 59))
    assert captured == {
        "mercado": "bCBA", "simbolo": "QA0", "cantidad": 10, "precio": 100.5,
        "validez": "2026-06-11T16:59:00", "tipoOrden": "precioLimite",
        "plazo": "t1", "idFuente": 0,
    }


# ── historico y helpers ───────────────────────────────────────────────────────

def test_historico_vacio_es_nodataerror():
    from datetime import date
    client, _ = make_client(lambda req: _json(200, []))
    with pytest.raises(IOLNoDataError):
        client.get_historico("bCBA", "QB7", date(2026, 3, 1), date(2026, 6, 1), ajustada="ajustada")


def test_historico_con_datos():
    from datetime import date
    bars = [{"ultimoPrecio": 100.0, "fechaHora": "2026-06-01T17:00:00"}]
    client, _ = make_client(lambda req: _json(200, bars))
    assert client.get_historico("bCBA", "QA0", date(2026, 3, 1), date(2026, 6, 1)) == bars


def test_get_operaciones_arma_filtros():
    from datetime import date
    captured = {}

    def handler(req):
        captured["params"] = dict(req.url.params)
        return _json(200, [])

    client, _ = make_client(handler)
    client.get_operaciones(estado="Todas", fecha_desde=date(2026, 6, 11), fecha_hasta=date(2026, 6, 11))
    assert captured["params"] == {
        "filtro.estado": "Todas",
        "filtro.fechaDesde": "2026-06-11",
        "filtro.fechaHasta": "2026-06-11",
    }


def test_normalize_pais():
    assert _normalize_pais("argentina") == "argentina"
    assert _normalize_pais("USA") == "estados_Unidos"
    assert _normalize_pais("Estados Unidos") == "estados_Unidos"
    with pytest.raises(ValueError):
        _normalize_pais("brasil")
