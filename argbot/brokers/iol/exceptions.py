class IOLError(Exception):
    pass


class IOLAuthError(IOLError):
    pass


# A failed login must say whether the connection dropped or the server refused it.
# The class names the kind, so
# `type(exc).__name__` carries it into the reconciliation reason, incidents and the
# Monitor without copying any response body.
class IOLAuthNetworkError(IOLAuthError):
    """No answer from /token: the connection failed or timed out."""


class IOLAuthServerError(IOLAuthError):
    """/token answered 5xx: a failure on IOL's side."""


class IOLAuthRejectedError(IOLAuthError):
    """IOL refused the credentials or the token: /token answered 4xx, or a 401 persisted."""


class IOLAPIError(IOLError):
    def __init__(self, status_code: int, message: str, body: str | None = None):
        self.status_code = status_code
        self.body = body
        super().__init__(f"IOL API error {status_code}: {message}")


class IOLNoDataError(IOLAPIError):
    """La API devolvió 0 barras para el instrumento/modo solicitado.

    Ocurre típicamente cuando se pide ajustada='ajustada' para instrumentos
    del panel general que no tienen datos ajustados en IOL.
    Usá ajustada='sinAjustar' explícitamente en ese caso.
    """
    def __init__(self, simbolo: str, mercado: str, ajustada: str):
        self.simbolo  = simbolo
        self.mercado  = mercado
        self.ajustada = ajustada
        super().__init__(
            0,
            f"No hay datos para {simbolo} ({mercado}) con ajustada='{ajustada}'. "
            f"Intentá con ajustada='sinAjustar'.",
        )
