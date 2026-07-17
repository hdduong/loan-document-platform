"""FastAPI adapter for the Azure-owned loan and document control plane."""

from __future__ import annotations

import asyncio
import base64
import binascii
import importlib
import inspect
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .auth import AuthProblem, JwtValidator, Principal, bearer_token, required_permission
from .aws_credentials import AwsFederatedSessionProvider, FederationError
from .settings import Settings, get_settings

LOGGER = logging.getLogger(__name__)
_SAFE_RESPONSE_HEADERS = {
    "cache-control",
    "content-disposition",
    "content-type",
    "etag",
    "location",
    "retry-after",
}


class DomainAdapterError(RuntimeError):
    """The framework-neutral loan API integration is not available."""


class DomainResponseError(RuntimeError):
    """The loan domain returned a response that the adapter cannot consume."""


class RequestBodyProblem(RuntimeError):
    """A bounded, sanitized request-body validation failure."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


@dataclass(slots=True)
class Runtime:
    settings: Settings
    validator: Any
    federation: Any
    domain_loader: Callable[[], Any]
    domain_lock: asyncio.Lock


def _load_domain_module() -> Any:
    """Load lazily so Azure startup never initializes boto3 with ambient credentials."""

    return importlib.import_module("services.loan_api.app")


def _problem(status: int, code: str, detail: str, correlation_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "type": f"https://api.loans.invalid/problems/{code.lower().replace('_', '-')}",
            "title": detail,
            "status": status,
            "detail": detail,
            "code": code,
            "correlationId": correlation_id,
            "errors": [],
        },
        media_type="application/problem+json",
    )


def _correlation_id(value: str | None) -> str:
    try:
        return str(uuid.UUID(value or ""))
    except (ValueError, TypeError, AttributeError):
        return str(uuid.uuid4())


def _request_hostname(request: Request) -> str:
    try:
        return (request.url.hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _envelope(request: Request, body: bytes, principal: Principal, correlation_id: str) -> dict[str, Any]:
    headers = {
        name: request.headers[name]
        for name in ("content-type", "idempotency-key")
        if name in request.headers
    }
    headers["x-correlation-id"] = correlation_id
    return {
        "version": "2.0",
        "rawPath": request.url.path,
        "rawQueryString": request.url.query,
        "headers": headers,
        "queryStringParameters": dict(request.query_params) or None,
        "body": body.decode("utf-8") if body else None,
        "isBase64Encoded": False,
        "requestContext": {
            "http": {"method": request.method, "path": request.url.path},
            "authorizer": {"jwt": {"claims": principal.safe_claims()}},
        },
    }


async def _bounded_body(request: Request, maximum_bytes: int) -> bytes:
    declared_header = request.headers.get("content-length")
    declared_length: int | None = None
    if declared_header is not None:
        try:
            declared_length = int(declared_header)
        except ValueError as exc:
            raise RequestBodyProblem(400, "INVALID_CONTENT_LENGTH", "Content-Length is invalid") from exc
        if declared_length < 0:
            raise RequestBodyProblem(400, "INVALID_CONTENT_LENGTH", "Content-Length is invalid")
        if declared_length > maximum_bytes:
            raise RequestBodyProblem(413, "REQUEST_TOO_LARGE", "Request body is too large")

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > maximum_bytes:
            raise RequestBodyProblem(413, "REQUEST_TOO_LARGE", "Request body is too large")
        body.extend(chunk)
    if declared_length is not None and len(body) != declared_length:
        raise RequestBodyProblem(400, "CONTENT_LENGTH_MISMATCH", "Content-Length does not match the body")
    return bytes(body)


def _call_domain(domain_loader: Callable[[], Any], session_context: Any, envelope: dict[str, Any]) -> Any:
    module = domain_loader()
    configure = getattr(module, "configure_aws_session", None)
    dispatch = getattr(module, "dispatch_request", None)
    if not callable(configure) or not callable(dispatch):
        raise DomainAdapterError("The loan domain adapter has not been installed")
    configure(session_context.session, credential_expiration=session_context.expiration)
    return dispatch(envelope)


def _validate_domain(domain_loader: Callable[[], Any]) -> None:
    module = domain_loader()
    configure = getattr(module, "configure_aws_session", None)
    dispatch = getattr(module, "dispatch_request", None)
    validate = getattr(module, "validate_runtime_configuration", None)
    if not callable(configure) or not callable(dispatch) or not callable(validate):
        raise DomainAdapterError("The loan domain readiness seam has not been installed")
    validate()


def _domain_response(result: Any) -> Response:
    if isinstance(result, Response):
        return result
    if not isinstance(result, Mapping) or "statusCode" not in result:
        if isinstance(result, (dict, list)):
            return JSONResponse(content=result)
        raise DomainAdapterError("The loan domain returned an unsupported response")

    status = int(result["statusCode"])
    headers = {
        str(name): str(value)
        for name, value in (result.get("headers") or {}).items()
        if str(name).casefold() in _SAFE_RESPONSE_HEADERS
    }
    body = result.get("body")
    if result.get("isBase64Encoded"):
        if not isinstance(body, str):
            raise DomainResponseError("The loan domain returned non-string Base64 content")
        try:
            content: bytes | str = base64.b64decode(body, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise DomainResponseError("The loan domain returned invalid Base64 content") from exc
    elif isinstance(body, (dict, list)):
        return JSONResponse(status_code=status, content=body, headers=headers)
    else:
        content = "" if body is None else str(body)
    return Response(status_code=status, content=content, headers=headers)


def _make_runtime(
    settings: Settings,
    validator: Any | None,
    federation: Any | None,
    domain_loader: Callable[[], Any],
) -> Runtime:
    return Runtime(
        settings=settings,
        validator=validator or JwtValidator(settings),
        federation=federation or AwsFederatedSessionProvider(settings),
        domain_loader=domain_loader,
        domain_lock=asyncio.Lock(),
    )


def create_app(
    settings: Settings | None = None,
    validator: Any | None = None,
    federation: Any | None = None,
    domain_loader: Callable[[], Any] = _load_domain_module,
) -> FastAPI:
    """Create an application with injectable external boundaries for deterministic tests."""

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        runtime = _make_runtime(settings or get_settings(), validator, federation, domain_loader)
        application.state.runtime = runtime
        try:
            yield
        finally:
            close = getattr(runtime.federation, "close", None)
            if callable(close):
                await run_in_threadpool(close)

    application = FastAPI(
        title="Loan Document API",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @application.middleware("http")
    async def request_boundary(request: Request, call_next: Callable[..., Any]) -> Response:
        runtime: Runtime = request.app.state.runtime
        correlation_id = _correlation_id(request.headers.get("x-correlation-id"))
        request.state.correlation_id = correlation_id
        origin = request.headers.get("origin")
        origin_allowed = origin in runtime.settings.allowed_origins if origin else False
        host_allowed = (
            runtime.settings.environment != "prod"
            or request.url.path in {"/health", "/ready"}
            or _request_hostname(request) == runtime.settings.api_host_name
        )

        if not host_allowed:
            response = _problem(421, "HOST_NOT_ALLOWED", "Request host is not allowed", correlation_id)
        elif origin and not origin_allowed:
            response = _problem(403, "ORIGIN_NOT_ALLOWED", "Request origin is not allowed", correlation_id)
        elif request.method == "OPTIONS":
            requested_method = request.headers.get("access-control-request-method", "")
            if not origin or required_permission(requested_method, request.url.path) is None:
                response = _problem(403, "PREFLIGHT_NOT_ALLOWED", "CORS preflight is not allowed", correlation_id)
            else:
                response = Response(status_code=204)
        else:
            response = await call_next(request)

        response.headers["x-correlation-id"] = correlation_id
        if origin_allowed:
            response.headers["access-control-allow-origin"] = origin or ""
            response.headers["access-control-allow-methods"] = "GET, POST, OPTIONS"
            response.headers["access-control-allow-headers"] = (
                "Authorization, Content-Type, Idempotency-Key, X-Correlation-Id"
            )
            response.headers["access-control-expose-headers"] = "Content-Disposition, X-Correlation-Id"
            response.headers["access-control-max-age"] = "600"
            response.headers.append("vary", "Origin")
        return response

    @application.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/ready", include_in_schema=False)
    async def ready(request: Request) -> Response:
        runtime: Runtime = request.app.state.runtime
        try:
            await run_in_threadpool(_validate_domain, runtime.domain_loader)
            # Prove the real managed-identity token claims and AWS trust before
            # a revision is considered ready. Normal probes reuse the provider's
            # cached credentials; credential values are never logged or returned.
            await run_in_threadpool(runtime.federation.session_context, 0)
        except Exception:
            LOGGER.error("runtime_readiness_failed")
            return JSONResponse(status_code=503, content={"status": "not_ready"})
        return JSONResponse(content={"status": "ready"})

    @application.api_route("/v1/{resource_path:path}", methods=["GET", "POST"], include_in_schema=False)
    async def product_api(request: Request, resource_path: str) -> Response:  # noqa: ARG001
        runtime: Runtime = request.app.state.runtime
        correlation_id = request.state.correlation_id
        permission = required_permission(request.method, request.url.path)
        if permission is None:
            return _problem(404, "ROUTE_NOT_FOUND", "Route was not found", correlation_id)

        try:
            token = bearer_token(request.headers.get("authorization"))
            principal = await run_in_threadpool(runtime.validator.validate, token, permission)
        except AuthProblem as problem:
            return _problem(problem.status_code, problem.code, problem.detail, correlation_id)

        try:
            body = await _bounded_body(request, runtime.settings.max_request_body_bytes)
        except RequestBodyProblem as problem:
            return _problem(problem.status, problem.code, problem.detail, correlation_id)
        try:
            envelope = _envelope(request, body, principal, correlation_id)
        except UnicodeDecodeError:
            return _problem(400, "INVALID_BODY", "Request body must be UTF-8", correlation_id)

        try:
            minimum_lifetime = (
                runtime.settings.max_grant_seconds + runtime.settings.aws_credential_refresh_seconds
            )
            # The domain keeps host-bound boto3 clients in module globals. Keep
            # configure + dispatch atomic within this single-worker process until
            # those clients move behind request-scoped repository instances.
            async with runtime.domain_lock:
                # Acquire after entering the lock so time spent waiting behind a
                # prior dispatch cannot consume the presigned-grant safety window.
                session_context = await run_in_threadpool(
                    runtime.federation.session_context, minimum_lifetime
                )
                result = await run_in_threadpool(
                    _call_domain, runtime.domain_loader, session_context, envelope
                )
                if inspect.isawaitable(result):
                    result = await result
            return _domain_response(result)
        except FederationError as problem:
            LOGGER.warning("aws_federation_failed code=%s correlation_id=%s", problem.code, correlation_id)
            return _problem(503, problem.code, "AWS integration is temporarily unavailable", correlation_id)
        except DomainAdapterError:
            LOGGER.error("domain_adapter_unavailable correlation_id=%s", correlation_id)
            return _problem(503, "DOMAIN_ADAPTER_NOT_READY", "The domain service is unavailable", correlation_id)
        except DomainResponseError:
            LOGGER.error("domain_response_invalid correlation_id=%s", correlation_id)
            return _problem(502, "DOMAIN_RESPONSE_INVALID", "The domain service returned an invalid response", correlation_id)
        except Exception:
            LOGGER.error("domain_dispatch_failed correlation_id=%s", correlation_id)
            return _problem(502, "DOMAIN_DEPENDENCY_ERROR", "The domain service request failed", correlation_id)

    return application


app = create_app()
