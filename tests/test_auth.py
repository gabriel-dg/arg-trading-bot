"""IOLAuth: parseo del /token real, márgenes de validez y ciclo login→refresh→re-login."""

import json
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime

import httpx
import pytest

from argbot.brokers.iol.auth import IOLAuth, TokenBundle, _bundle_from_response
from argbot.brokers.iol.exceptions import IOLAuthError, IOLAuthNetworkError, IOLAuthRejectedError, IOLAuthServerError


def _rfc1123(dt: datetime) -> str:
    return format_datetime(dt.astimezone(timezone.utc), usegmt=True)


def _token_body(access="acc-1", refresh="ref-1", mins_access=15, mins_refresh=20) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "access_token": access,
        "token_type": "bearer",
        "expires_in": 1200,  # contradice .expires a propósito (como la API real)
        "refresh_token": refresh,
        ".issued": _rfc1123(now),
        ".expires": _rfc1123(now + timedelta(minutes=mins_access)),
        ".refreshexpires": _rfc1123(now + timedelta(minutes=mins_refresh)),
    }


# ── parseo de la respuesta real de /token ─────────────────────────────────────

def test_bundle_confia_en_expires_no_en_expires_in():
    body = _token_body(mins_access=15)
    bundle = _bundle_from_response(body)
    esperado = parsedate_to_datetime(body[".expires"]).timestamp()
    assert bundle.access_expires_at == esperado
    # .expires (15 min) gana sobre expires_in (20 min)
    assert bundle.access_expires_at < time.time() + 1200


def test_bundle_refreshexpires_sin_underscore():
    bundle = _bundle_from_response(_token_body(mins_refresh=20))
    assert bundle.refresh_expires_at > bundle.access_expires_at


def test_bundle_sin_refreshexpires_usa_fallback():
    body = _token_body()
    del body[".refreshexpires"]
    bundle = _bundle_from_response(body)
    assert bundle.refresh_expires_at == bundle.access_expires_at + 300


def test_bundle_sin_access_token_falla():
    with pytest.raises(IOLAuthError):
        _bundle_from_response({"refresh_token": "x"})


def test_token_bundle_margen_60s():
    ahora = time.time()
    casi_vencido = TokenBundle("a", "r", ahora + 30, ahora + 30)
    vigente = TokenBundle("a", "r", ahora + 120, ahora + 120)
    assert casi_vencido.access_valid() is False, "30s restantes < margen de 60s"
    assert vigente.access_valid() is True


# ── ciclo completo con /token mockeado ────────────────────────────────────────

class TokenServer:
    """Handler de httpx.MockTransport que registra los grants recibidos."""

    def __init__(self):
        self.grants: list[str] = []
        self.fail_refresh = False
        self.serial = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/token"
        form = dict(p.split("=", 1) for p in req.content.decode().split("&"))
        grant = form["grant_type"]
        self.grants.append(grant)
        if grant == "refresh_token" and self.fail_refresh:
            return httpx.Response(400, json={"error": "invalid_grant"})
        self.serial += 1
        return httpx.Response(200, json=_token_body(access=f"acc-{self.serial}",
                                                    refresh=f"ref-{self.serial}"))


def _make_auth(tmp_path, server) -> IOLAuth:
    return IOLAuth(
        username="user", password="pass", env="production",
        token_path=tmp_path / ".iol_token.json",
        http_client=httpx.Client(transport=httpx.MockTransport(server)),
    )


def test_login_refresh_y_relogin(tmp_path):
    server = TokenServer()
    auth = _make_auth(tmp_path, server)

    headers = auth.headers()
    assert headers["Authorization"].endswith("acc-1")
    assert server.grants == ["password"]

    # access vencido, refresh vigente → refresh
    auth._token.access_expires_at = 0
    auth.headers()
    assert server.grants == ["password", "refresh_token"]
    assert auth.last_grant_used == "refresh_token"

    # refresh falla → fallback a login completo (sin fallo silencioso)
    server.fail_refresh = True
    auth._token.access_expires_at = 0
    auth.headers()
    assert server.grants == ["password", "refresh_token", "refresh_token", "password"]
    assert auth.last_grant_used == "password"


def test_token_persistido_se_reusa_sin_red(tmp_path):
    server = TokenServer()
    auth = _make_auth(tmp_path, server)
    auth.headers()
    assert (tmp_path / ".iol_token.json").exists()

    def explota(req):
        raise AssertionError("no debe llamar a la red: token válido en disco")

    auth2 = IOLAuth(
        username="user", password="pass", env="production",
        token_path=tmp_path / ".iol_token.json",
        http_client=httpx.Client(transport=httpx.MockTransport(explota)),
    )
    assert auth2.loaded_from_disk is True
    assert auth2.headers()["Authorization"].endswith("acc-1")


def test_token_persistido_vencido_se_descarta(tmp_path):
    vencido = TokenBundle("a", "r", time.time() - 100, time.time() - 50)
    from dataclasses import asdict
    (tmp_path / ".iol_token.json").write_text(json.dumps(asdict(vencido)), encoding="utf-8")

    server = TokenServer()
    auth = _make_auth(tmp_path, server)
    assert auth.loaded_from_disk is False
    auth.headers()
    assert server.grants == ["password"], "refresh vencido → login completo directo"


def test_token_file_corrupto_se_ignora(tmp_path):
    (tmp_path / ".iol_token.json").write_text("{basura", encoding="utf-8")
    server = TokenServer()
    auth = _make_auth(tmp_path, server)
    assert auth.loaded_from_disk is False
    auth.headers()
    assert server.grants == ["password"]


def test_sin_credenciales_falla_claro(tmp_path, monkeypatch):
    monkeypatch.delenv("IOL_USERNAME", raising=False)
    monkeypatch.delenv("IOL_PASSWORD", raising=False)
    auth = IOLAuth(env="production", token_path=tmp_path / ".iol_token.json",
                   http_client=httpx.Client(transport=httpx.MockTransport(TokenServer())))
    with pytest.raises(IOLAuthError, match="credenciales"):
        auth.headers()


# ── qué tipo de falla tuvo /token ───────────────────────────────────────────
# Un ingreso fallido puede bloquear varios minutos; el registro debe
# distinguir un corte de un rechazo. La clase lo dice, sin copiar la respuesta.

def _drop(error):
    def respond(req):
        raise error("caída simulada", request=req)
    return respond


@pytest.mark.parametrize("respond,kind", [
    (_drop(httpx.ConnectError), IOLAuthNetworkError),
    (_drop(httpx.ReadTimeout), IOLAuthNetworkError),
    (lambda req: httpx.Response(503, text="mantenimiento"), IOLAuthServerError),
    (lambda req: httpx.Response(500, json={}), IOLAuthServerError),
    (lambda req: httpx.Response(400, json={"error": "invalid_grant"}), IOLAuthRejectedError),
    (lambda req: httpx.Response(401, json={}), IOLAuthRejectedError),
    (lambda req: httpx.Response(429, json={}), IOLAuthRejectedError),
    (lambda req: httpx.Response(302, headers={"Location": "/otra"}), IOLAuthError),
    (lambda req: httpx.Response(200, text="no es json"), IOLAuthError),
    (lambda req: httpx.Response(200, json={"token_type": "bearer"}), IOLAuthError),
], ids=["sin_conexion", "sin_respuesta", "503", "500", "400", "401", "429", "302",
        "200_no_json", "200_sin_token"])
def test_la_falla_de_token_dice_de_que_tipo_es(tmp_path, respond, kind):
    with pytest.raises(IOLAuthError) as caught:
        _make_auth(tmp_path, respond).headers()
    assert type(caught.value) is kind


def test_el_tipo_lo_da_el_codigo_nunca_el_texto_de_la_respuesta(tmp_path):
    body = {"error": "Error de red en /token", "error_description": "503"}
    with pytest.raises(IOLAuthError) as caught:
        _make_auth(tmp_path, lambda req: httpx.Response(400, json=body)).headers()
    assert type(caught.value) is IOLAuthRejectedError


def test_refresh_rechazado_y_login_sin_conexion_es_falla_de_red(tmp_path):
    # El refresh falla, el bot prueba con usuario y clave y el
    # ingreso no llega. Lo que se informa es el último intento.
    server, down = TokenServer(), {"on": False}

    def respond(req):
        if not down["on"]:
            return server(req)
        form = dict(p.split("=", 1) for p in req.content.decode().split("&"))
        if form["grant_type"] == "refresh_token":
            return httpx.Response(400, json={"error": "invalid_grant"})
        raise httpx.ConnectError("caída simulada", request=req)

    auth = _make_auth(tmp_path, respond)
    auth.headers()
    auth.invalidate_access_token()
    down["on"] = True
    with pytest.raises(IOLAuthNetworkError):
        auth.headers()


def test_las_clases_nuevas_siguen_siendo_iolautherror():
    for kind in (IOLAuthNetworkError, IOLAuthServerError, IOLAuthRejectedError):
        assert issubclass(kind, IOLAuthError)


def test_default_keeps_tokens_in_memory_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    server = TokenServer()
    auth = IOLAuth(
        username="user", password="pass", env="production",
        http_client=httpx.Client(transport=httpx.MockTransport(server)),
    )
    assert auth.headers()["Authorization"].endswith("acc-1")
    assert not (tmp_path / ".iol_token.json").exists()
    assert list(tmp_path.glob("*.json")) == []
    auth.close()


def test_existing_token_file_is_ignored_without_token_path(tmp_path, monkeypatch):
    from dataclasses import asdict
    monkeypatch.chdir(tmp_path)
    leftover = TokenBundle("secret-access", "secret-refresh", time.time() + 3600, time.time() + 7200)
    (tmp_path / ".iol_token.json").write_text(json.dumps(asdict(leftover)), encoding="utf-8")
    server = TokenServer()
    auth = IOLAuth(
        username="user", password="pass", env="production",
        http_client=httpx.Client(transport=httpx.MockTransport(server)),
    )
    assert auth.loaded_from_disk is False
    assert auth.headers()["Authorization"].endswith("acc-1")
    assert json.loads((tmp_path / ".iol_token.json").read_text())["access_token"] == "secret-access"
    auth.close()


def test_token_error_does_not_include_the_response_body(tmp_path):
    secret = "super-secret-body-token-value"
    with pytest.raises(IOLAuthError) as caught:
        _make_auth(tmp_path, lambda req: httpx.Response(400, text=secret)).headers()
    assert secret not in str(caught.value)
    assert "400" in str(caught.value)
