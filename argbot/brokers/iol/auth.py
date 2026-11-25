from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

import httpx

from argbot.brokers.iol.exceptions import (
    IOLAuthError,
    IOLAuthNetworkError,
    IOLAuthRejectedError,
    IOLAuthServerError,
)

logger = logging.getLogger("argbot.brokers.iol.auth")

_PROD_URL = "https://api.invertironline.com"
# IOL's homologation host. `argbot live` never uses it (it always selects production),
# and this project does not verify that it works.
_SANDBOX_URL = "https://api.homo.invertironline.com"
_REFRESH_MARGIN_SECONDS = 60

def base_url_for_env(env: str) -> str:
    env = (env or "production").strip().lower()
    if env == "production":
        return _PROD_URL
    if env == "sandbox":
        return _SANDBOX_URL
    raise ValueError(f"IOL_ENV inválido: {env!r} (esperado 'production' o 'sandbox')")


@dataclass
class TokenBundle:
    access_token: str
    refresh_token: str
    access_expires_at: float
    refresh_expires_at: float
    token_type: str = "Bearer"

    def access_valid(self, margin: float = _REFRESH_MARGIN_SECONDS) -> bool:
        return time.time() + margin < self.access_expires_at

    def refresh_valid(self, margin: float = _REFRESH_MARGIN_SECONDS) -> bool:
        return time.time() + margin < self.refresh_expires_at


class IOLAuth:
    """IOL token grant. Tokens stay in memory unless `token_path` is set.

    Passing `token_path` is an explicit opt-in to a plaintext JSON token cache.
    The file is not encrypted, and `chmod 0600` has no effect on Windows, so treat
    that path as sensitive and never commit it.
    """

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        env: Optional[str] = None,
        token_path: Path | str | None = None,
        http_client: Optional[httpx.Client] = None,
    ):
        self.username = username or os.environ.get("IOL_USERNAME")
        self.password = password or os.environ.get("IOL_PASSWORD")
        self.base_url = base_url_for_env(env or os.environ.get("IOL_ENV", "production"))
        # None (the default) keeps tokens in memory only. A path is an opt-in cache.
        self.token_path = Path(token_path) if token_path is not None else None
        self._http = http_client or httpx.Client(timeout=30.0)
        self._owns_http = http_client is None
        self.loaded_from_disk: bool = False
        self.last_grant_used: Optional[str] = None
        self.last_token_response_keys: Optional[list[str]] = None
        self.last_access_expires_raw: Optional[str] = None
        self.last_refresh_expires_raw: Optional[str] = None
        self._token: Optional[TokenBundle] = self._load_token()
        self._persist_warned = False

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> "IOLAuth":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def headers(self) -> dict[str, str]:
        token = self._ensure_valid_token()
        return {
            "Authorization": f"{token.token_type} {token.access_token}",
            "Accept": "application/json",
        }

    def invalidate_access_token(self) -> None:
        if self._token is not None:
            self._token.access_expires_at = 0

    def _ensure_valid_token(self) -> TokenBundle:
        if self._token and self._token.access_valid():
            return self._token
        if self._token and self._token.refresh_valid():
            try:
                return self._refresh()
            except IOLAuthError:
                logger.warning("Refresh falló; reintento con login completo")
        return self._login()

    def _login(self) -> TokenBundle:
        if not self.username or not self.password:
            raise IOLAuthError("Faltan credenciales: IOL_USERNAME / IOL_PASSWORD")
        logger.info("POST /token grant=password")
        token = self._token_request(
            {
                "username": self.username,
                "password": self.password,
                "grant_type": "password",
            }
        )
        self.last_grant_used = "password"
        self._token = token
        self._save_token(token)
        return token

    def _refresh(self) -> TokenBundle:
        assert self._token is not None
        logger.info("POST /token grant=refresh_token")
        token = self._token_request(
            {
                "refresh_token": self._token.refresh_token,
                "grant_type": "refresh_token",
            }
        )
        self.last_grant_used = "refresh_token"
        self._token = token
        self._save_token(token)
        return token

    def _token_request(self, payload: dict) -> TokenBundle:
        url = f"{self.base_url}/token"
        started = time.monotonic()
        try:
            resp = self._http.post(
                url,
                data=payload,
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise IOLAuthNetworkError(f"Error de red en /token: {exc}") from exc
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.info("POST /token -> %s (%.0f ms)", resp.status_code, elapsed_ms)
        if resp.status_code != 200:
            # The kind comes from the status code only, never from the body.
            kind = (IOLAuthServerError if resp.status_code >= 500
                    else IOLAuthRejectedError if resp.status_code >= 400 else IOLAuthError)
            raise kind(
                f"Token endpoint devolvió {resp.status_code}"
            )
        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise IOLAuthError(f"Respuesta no-JSON de /token: {exc}") from exc
        self.last_token_response_keys = list(data.keys())
        self.last_access_expires_raw = data.get(".expires")
        self.last_refresh_expires_raw = data.get(".refreshexpires") or data.get(".refresh_token_expires")
        return _bundle_from_response(data)

    def _load_token(self) -> Optional[TokenBundle]:
        if self.token_path is None or not self.token_path.exists():
            return None
        try:
            data = json.loads(self.token_path.read_text(encoding="utf-8"))
            token = TokenBundle(**data)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("Token file inválido (%s); se ignorará", exc)
            return None
        if not token.refresh_valid():
            logger.info("Token persistido vencido; se hará login completo")
            return None
        logger.debug("Token cargado desde %s", self.token_path)
        self.loaded_from_disk = True
        return token

    def _save_token(self, token: TokenBundle) -> None:
        if self.token_path is None:
            return
        if not self._persist_warned:
            logger.warning(
                "Persisting IOL tokens in plaintext at %s. The file is not encrypted, "
                "and chmod 0600 has no effect on Windows.",
                self.token_path,
            )
            self._persist_warned = True
        try:
            self.token_path.write_text(
                json.dumps(asdict(token), indent=2),
                encoding="utf-8",
            )
            try:
                os.chmod(self.token_path, 0o600)
            except OSError:
                pass
        except OSError as exc:
            logger.warning("No se pudo persistir token: %s", exc)


def _bundle_from_response(data: dict) -> TokenBundle:
    try:
        access_token = data["access_token"]
        refresh_token = data["refresh_token"]
    except KeyError as exc:
        raise IOLAuthError(f"Respuesta de /token sin campo {exc}") from exc

    now = time.time()

    # IOL devuelve `expires_in` (1200s) y `.expires` (15min) que se contradicen.
    # Usamos `.expires` por ser el valor conservador — evita seguir mandando un
    # token que el server ya rechaza.
    access_expires_at = _parse_iol_datetime(data.get(".expires"))
    if access_expires_at is None:
        expires_in = int(data.get("expires_in", 900))
        access_expires_at = now + expires_in

    refresh_expires_raw = data.get(".refreshexpires") or data.get(".refresh_token_expires")
    refresh_expires_at = _parse_iol_datetime(refresh_expires_raw)
    if refresh_expires_at is None:
        logger.warning("No se encontró campo de expiración del refresh; usando fallback de 5min después del access")
        refresh_expires_at = access_expires_at + 5 * 60

    return TokenBundle(
        access_token=access_token,
        refresh_token=refresh_token,
        access_expires_at=access_expires_at,
        refresh_expires_at=refresh_expires_at,
        token_type=data.get("token_type", "Bearer"),
    )


def _parse_iol_datetime(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
