import hmac
from ipaddress import IPv6Address, ip_address

from starlette.requests import Request

from app.core.config import Settings


def normalize_client_ip(value: str) -> str | None:
    try:
        address = ip_address(value.strip())
    except ValueError:
        return None
    if isinstance(address, IPv6Address):
        if address.scope_id is not None:
            return None
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
    return str(address)


def single_header_ip(request: Request, name: str) -> str | None:
    values = request.headers.getlist(name)
    return normalize_client_ip(values[0]) if len(values) == 1 else None


def resolve_client_ip(request: Request, settings: Settings) -> str:
    configured = settings.proxy_shared_secret
    supplied = request.headers.getlist("x-proxy-secret")
    if (configured is not None and len(supplied) == 1
            and hmac.compare_digest(configured.get_secret_value().encode(), supplied[0].encode())):
        if forwarded := single_header_ip(request, "x-client-ip"):
            return forwarded
    if settings.proxy_trusted_edge == "cloudbase":
        if edge := single_header_ip(request, "x-envoy-external-address"):
            return edge
    peer = normalize_client_ip(request.client.host) if request.client is not None else None
    return peer or "unknown"
