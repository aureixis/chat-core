from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True)
class RequestContext:
    request_id: str | None = None
    ip_address: str | None = None


_request_context: ContextVar[RequestContext | None] = ContextVar(
    "lamya_request_context", default=None
)


def set_request_context(request_id: str, ip_address: str | None) -> Token:
    return _request_context.set(RequestContext(request_id=request_id, ip_address=ip_address))


def reset_request_context(token: Token) -> None:
    _request_context.reset(token)


def get_request_context() -> RequestContext:
    return _request_context.get() or RequestContext()
