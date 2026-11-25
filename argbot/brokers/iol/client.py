from __future__ import annotations

import logging
import time
from datetime import date, datetime
from typing import Any, Literal, Optional

import httpx

from argbot.brokers.iol.auth import IOLAuth
from argbot.brokers.iol.exceptions import IOLAPIError, IOLAuthRejectedError, IOLNoDataError

logger = logging.getLogger("argbot.brokers.iol.client")

_PAIS_ALIASES = {
    "argentina": "argentina",
    "estados_unidos": "estados_Unidos",
    "estados unidos": "estados_Unidos",
    "eeuu": "estados_Unidos",
    "usa": "estados_Unidos",
}


def _normalize_pais(pais: str) -> str:
    key = pais.strip().lower().replace(" ", "_")
    if key not in _PAIS_ALIASES:
        raise ValueError(
            f"pais inválido: {pais!r}. Aceptados: argentina | estados_unidos (case-insensitive)"
        )
    return _PAIS_ALIASES[key]


def _fmt_fecha(d: date | datetime) -> str:
    return d.date().isoformat() if isinstance(d, datetime) else d.isoformat()


def _size_summary(data: Any, byte_count: int) -> str:
    if isinstance(data, list):
        return f"{len(data)} items"
    if isinstance(data, dict):
        for key in ("activos", "titulos", "cuentas", "items", "data"):
            val = data.get(key)
            if isinstance(val, list):
                return f"{len(val)} {key}"
        return f"{byte_count} bytes"
    return f"{byte_count} bytes"


class IOLClient:
    def __init__(
        self,
        auth: Optional[IOLAuth] = None,
        http_client: Optional[httpx.Client] = None,
    ):
        self.auth = auth or IOLAuth()
        self._http = http_client or httpx.Client(timeout=30.0)
        self._owns_http = http_client is None
        self._owns_auth = auth is None

    def close(self) -> None:
        if self._owns_http:
            self._http.close()
        if self._owns_auth:
            self.auth.close()

    def __enter__(self) -> "IOLClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def get_estado_cuenta(self) -> dict:
        return self._request("GET", "/api/v2/estadocuenta")

    def get_portafolio(self, pais: str = "argentina") -> dict:
        return self._request("GET", f"/api/v2/portafolio/{_normalize_pais(pais)}")

    def get_cotizacion(self, mercado: Literal["bCBA", "nYSE", "nASDAQ", "aMEX", "bCS", "rOFX"], simbolo: str, plazo: str) -> dict:
        return self._request(
            "GET",
            f"/api/v2/{mercado}/Titulos/{simbolo}/Cotizacion",
            params={"plazo": plazo},
        )

    def get_historico(
        self,
        mercado: Literal["bCBA", "nYSE", "nASDAQ", "aMEX", "bCS", "rOFX"],
        simbolo: str,
        fecha_desde: date | datetime,
        fecha_hasta: date | datetime,
        ajustada: Literal["ajustada", "sinAjustar"] = "sinAjustar",
    ) -> list:
        """Serie histórica de cotizaciones.

        Args:
            mercado: p.ej. "bCBA" para BCBA. Ver Literal para valores válidos.
            simbolo: ticker del título.
            fecha_desde / fecha_hasta: date o datetime — el cliente formatea como YYYY-MM-DD.
            ajustada: "ajustada" → un item por día de rueda (cierre ajustado).
                      "sinAjustar" → ticks intradiarios (granularidad sub-minuto).

        Returns:
            Lista de cotizaciones [{ultimoPrecio, apertura, maximo, minimo, fechaHora, ...}, ...].
            fechaHora es string ISO 8601.

        Raises:
            IOLNoDataError: si la API devuelve 0 barras. Ocurre con ajustada='ajustada'
                para algunos instrumentos del panel general que no tienen
                datos ajustados en IOL. En ese caso usá ajustada='sinAjustar' explícitamente.
        """
        data = self._request(
            "GET",
            f"/api/v2/{mercado}/Titulos/{simbolo}/Cotizacion/seriehistorica"
            f"/{_fmt_fecha(fecha_desde)}/{_fmt_fecha(fecha_hasta)}/{ajustada}",
        )
        result = data if isinstance(data, list) else data.get("bars", data)
        if not result:
            raise IOLNoDataError(simbolo, mercado, ajustada)
        return result

    @staticmethod
    def _check_operar_response(data: Any, op: str) -> dict:
        # Vender devuelve lista plana, Comprar devuelve {ok, messages} — ambos son errores de negocio
        msgs_list = None
        if isinstance(data, list) and data:
            msgs_list = data
        elif isinstance(data, dict) and not data.get("ok", True):
            msgs_list = data.get("messages", [])
        if msgs_list is not None:
            msgs = "; ".join(m.get("description", "").strip() for m in msgs_list if m.get("description"))
            raise IOLAPIError(200, f"{op}: {msgs}")
        return data

    def comprar(
        self,
        mercado: Literal["bCBA", "nYSE", "nASDAQ", "aMEX", "bCS", "rOFX"],
        simbolo: str,
        cantidad: int,
        precio: float,
        plazo: str,
        validez: date | datetime,
        tipo_orden: str = "precioLimite",
    ) -> dict:
        if isinstance(validez, datetime):
            validez_str = validez.isoformat()
        else:
            validez_str = datetime(validez.year, validez.month, validez.day, 23, 59, 59).isoformat()
        data = self._request(
            "POST",
            "/api/v2/operar/Comprar",
            json_body={
                "mercado": mercado,
                "simbolo": simbolo,
                "cantidad": cantidad,
                "precio": precio,
                "validez": validez_str,
                "tipoOrden": tipo_orden,
                "plazo": plazo,
                "idFuente": 0,
            },
        )
        return self._check_operar_response(data, "POST /api/v2/operar/Comprar")

    def vender(
        self,
        mercado: Literal["bCBA", "nYSE", "nASDAQ", "aMEX", "bCS", "rOFX"],
        simbolo: str,
        cantidad: int,
        precio: float,
        plazo: str,
        validez: date | datetime,
        tipo_orden: str = "precioLimite",
    ) -> dict:
        if isinstance(validez, datetime):
            validez_str = validez.isoformat()
        else:
            validez_str = datetime(validez.year, validez.month, validez.day, 23, 59, 59).isoformat()
        data = self._request(
            "POST",
            "/api/v2/operar/Vender",
            json_body={
                "mercado": mercado,
                "simbolo": simbolo,
                "cantidad": cantidad,
                "precio": precio,
                "validez": validez_str,
                "tipoOrden": tipo_orden,
                "plazo": plazo,
                "idFuente": 0,
            },
        )
        # Errores de validación de negocio llegan como HTTP 200 con lista de mensajes
        # (el PDF documenta {ok, messages} pero la API real devuelve lista plana)
        return self._check_operar_response(data, "POST /api/v2/operar/Vender")

    def get_operacion(self, numero: int) -> dict:
        return self._request("GET", f"/api/v2/operaciones/{numero}")

    def get_operaciones(
        self,
        numero: Optional[int] = None,
        estado: Optional[str] = None,
        fecha_desde: Optional[date | datetime] = None,
        fecha_hasta: Optional[date | datetime] = None,
        pais: Optional[str] = None,
    ) -> list:
        """Lista de operaciones con filtros opcionales.

        estado: "Todas" | "Pendientes" | "Terminadas" | "Canceladas"
        """
        params: dict = {}
        if numero is not None:
            params["filtro.numero"] = numero
        if estado is not None:
            params["filtro.estado"] = estado
        if fecha_desde is not None:
            params["filtro.fechaDesde"] = _fmt_fecha(fecha_desde)
        if fecha_hasta is not None:
            params["filtro.fechaHasta"] = _fmt_fecha(fecha_hasta)
        if pais is not None:
            params["filtro.pais"] = pais
        return self._request("GET", "/api/v2/operaciones", params=params or None)

    def cancelar_orden(self, numero: int) -> dict:
        return self._request("DELETE", f"/api/v2/operaciones/{numero}")

    def get_opciones(self, mercado: Literal["bCBA", "nYSE", "nASDAQ", "aMEX", "bCS", "rOFX"], subyacente: str) -> list:
        """Cadena de opciones para un subyacente.

        Returns:
            Lista de contratos. Cada elemento tiene:
            simboloSubyacente, fechaVencimiento, tipoOpcion ("Call"/"Put"),
            simbolo, descripcion, mercado, plazo, moneda,
            cotizacion (sub-objeto con ultimoPrecio, apertura, etc. — ceros fuera de horario).
        """
        return self._request(
            "GET",
            f"/api/v2/{mercado}/Titulos/{subyacente}/Opciones",
        )

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
    ) -> Any:
        return self._do_request(method, path, params, json_body, retried=False)

    def _do_request(
        self,
        method: str,
        path: str,
        params: Optional[dict],
        json_body: Optional[dict],
        retried: bool,
    ) -> Any:
        url = f"{self.auth.base_url}{path}"
        headers = self.auth.headers()

        started = time.monotonic()
        try:
            resp = self._http.request(method, url, params=params, json=json_body, headers=headers)
        except httpx.HTTPError as exc:
            raise IOLAPIError(0, f"Error de red en {method} {path}: {exc}") from exc
        elapsed_ms = (time.monotonic() - started) * 1000

        if resp.status_code == 401 and not retried:
            logger.warning(
                "%s %s -> 401 (%.0fms); invalidando access token y reintentando una vez",
                method, path, elapsed_ms,
            )
            self.auth.invalidate_access_token()
            return self._do_request(method, path, params, json_body, retried=True)

        body_bytes = len(resp.content)

        if resp.status_code >= 400:
            logger.info(
                "%s %s -> %s (%.0fms, %d bytes)",
                method, path, resp.status_code, elapsed_ms, body_bytes,
            )
            if resp.status_code == 401:
                raise IOLAuthRejectedError(
                    f"401 persistente después de refresh+retry en {method} {path}"
                )
            raise IOLAPIError(
                resp.status_code,
                f"{method} {path}",
            )

        try:
            data = resp.json()
        except ValueError as exc:
            logger.info(
                "%s %s -> %s (%.0fms, %d bytes, no-JSON)",
                method, path, resp.status_code, elapsed_ms, body_bytes,
            )
            raise IOLAPIError(
                resp.status_code,
                f"Respuesta no-JSON en {method} {path}",
            ) from exc

        logger.info(
            "%s %s -> %s (%.0fms, %s)",
            method, path, resp.status_code, elapsed_ms, _size_summary(data, body_bytes),
        )
        return data
