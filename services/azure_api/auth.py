"""Cryptographic Entra access-token and route-permission validation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

import jwt

from .settings import Settings


class AuthProblem(Exception):
    """A sanitized authentication or authorization failure."""

    def __init__(self, status_code: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class Principal:
    tenant_id: str
    actor_id: str
    client_id: str
    actor_type: str
    scopes: frozenset[str]
    roles: frozenset[str]

    def safe_claims(self) -> dict[str, Any]:
        """Return only claims required by the existing domain authorization seam."""

        claims: dict[str, Any] = {
            "tid": self.tenant_id,
            "oid": self.actor_id,
            "azp": self.client_id,
            "roles": sorted(self.roles),
        }
        if self.actor_type == "servicePrincipal":
            claims["idtyp"] = "app"
        else:
            claims["scp"] = " ".join(sorted(self.scopes))
        return claims


_PERMISSION_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("POST", re.compile(r"^/v1/loans$"), "Loan.Create"),
    ("POST", re.compile(r"^/v1/loans/[^/]+/archive$"), "Loan.Archive"),
    ("POST", re.compile(r"^/v1/loans/[^/]+/documents$"), "Document.Upload"),
    ("POST", re.compile(r"^/v1/loans/[^/]+/documents/[^/]+/uploads$"), "Document.Upload"),
    (
        "POST",
        re.compile(r"^/v1/loans/[^/]+/documents/[^/]+/uploads/[^/]+/complete$"),
        "Document.Upload",
    ),
    ("POST", re.compile(r"^/v1/loans/[^/]+/documents/[^/]+/archive$"), "Document.Archive"),
    ("GET", re.compile(r"^/v1/loans/[^/]+(?:/archives/[^/]+)?$"), "Loan.Read"),
    (
        "GET",
        re.compile(
            r"^/v1/loans/[^/]+/(?:archives/[^/]+/)?documents/[^/]+"
            r"(?:/archives/[^/]+)?/data-points(?:/download)?$"
        ),
        "DataPoints.Read",
    ),
    (
        "GET",
        re.compile(
            r"^/v1/loans/[^/]+/(?:archives/[^/]+/)?documents/[^/]+"
            r"(?:/archives/[^/]+)?(?:/download)?$"
        ),
        "Document.Read",
    ),
)


def required_permission(method: str, path: str) -> str | None:
    """Resolve an exact product route to its Entra permission."""

    normalized_method = method.upper()
    for candidate_method, pattern, permission in _PERMISSION_RULES:
        if normalized_method == candidate_method and pattern.fullmatch(path):
            return permission
    return None


def bearer_token(authorization: str | None) -> str:
    parts = (authorization or "").strip().split()
    if len(parts) != 2 or parts[0].casefold() != "bearer" or not parts[1]:
        raise AuthProblem(401, "TOKEN_REQUIRED", "A bearer access token is required")
    return parts[1]


def _claim_values(value: Any) -> frozenset[str]:
    if isinstance(value, list):
        return frozenset(item.strip() for item in value if isinstance(item, str) and item.strip())
    if isinstance(value, str):
        return frozenset(item for item in re.split(r"[\s,]+", value.strip()) if item)
    return frozenset()


_APPLICATION_ROLE_SUFFIX = ".Role"


def _permission_roles(value: Any) -> frozenset[str]:
    """Normalize collision-free Entra app-role values to domain permissions."""

    return frozenset(
        role[: -len(_APPLICATION_ROLE_SUFFIX)]
        for role in _claim_values(value)
        if role.endswith(_APPLICATION_ROLE_SUFFIX) and role != _APPLICATION_ROLE_SUFFIX
    )


class JwtValidator:
    """Validate Entra JWTs and enforce the platform's route authorization rules."""

    def __init__(
        self,
        settings: Settings,
        signing_key_resolver: Callable[[str], Any] | None = None,
    ) -> None:
        self._settings = settings
        self._jwk_client = jwt.PyJWKClient(
            settings.entra_jwks_uri,
            cache_keys=True,
            lifespan=300,
            timeout=settings.jwks_timeout_seconds,
        )
        self._resolve_key = signing_key_resolver or self._resolve_signing_key

    def _resolve_signing_key(self, token: str) -> Any:
        return self._jwk_client.get_signing_key_from_jwt(token).key

    def validate(self, token: str, required_permission_name: str) -> Principal:
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not header.get("kid"):
                raise AuthProblem(401, "TOKEN_INVALID", "The access token header is invalid")
            claims = jwt.decode(
                token,
                self._resolve_key(token),
                algorithms=["RS256"],
                audience=self._settings.entra_api_audience,
                issuer=self._settings.entra_issuer,
                leeway=self._settings.jwt_leeway_seconds,
                options={
                    "require": ["aud", "exp", "iat", "iss", "nbf", "sub", "tid"],
                    "strict_aud": True,
                },
            )
        except AuthProblem:
            raise
        except (jwt.PyJWTError, TypeError, ValueError) as exc:
            raise AuthProblem(401, "TOKEN_INVALID", "The access token is invalid or expired") from exc

        tenant_id = str(claims.get("tid", ""))
        if tenant_id.casefold() != self._settings.entra_tenant_id.casefold():
            raise AuthProblem(403, "TENANT_NOT_ALLOWED", "The token tenant is not allowed")

        client_id = str(claims.get("azp") or claims.get("appid") or "")
        normalized_client_id = client_id.casefold()
        if not client_id or normalized_client_id in self._settings.denied_client_ids:
            raise AuthProblem(403, "CLIENT_NOT_ALLOWED", "The calling application is not allowed")
        if normalized_client_id not in self._settings.allowed_client_ids:
            raise AuthProblem(403, "CLIENT_NOT_ALLOWED", "The calling application is not allowlisted")

        actor_id = str(claims.get("oid") or claims.get("sub") or "")
        if not actor_id:
            raise AuthProblem(403, "ACTOR_REQUIRED", "The token has no immutable actor identifier")

        scopes = _claim_values(claims.get("scp"))
        roles = _permission_roles(claims.get("roles"))
        required_role_value = f"{required_permission_name}{_APPLICATION_ROLE_SUFFIX}"
        if scopes:
            if claims.get("idtyp") == "app":
                raise AuthProblem(403, "TOKEN_TYPE_NOT_ALLOWED", "An app-only token cannot use delegated scopes")
            if required_permission_name not in scopes:
                raise AuthProblem(403, "SCOPE_REQUIRED", f"Required delegated scope: {required_permission_name}")
            if self._settings.require_user_roles and required_permission_name not in roles:
                raise AuthProblem(403, "ROLE_REQUIRED", f"Required assigned app role: {required_role_value}")
            actor_type = "user"
        else:
            if claims.get("idtyp") != "app":
                raise AuthProblem(403, "TOKEN_TYPE_NOT_ALLOWED", "An app-only token must contain idtyp=app")
            if str(claims.get("azpacr", "")) != "2":
                raise AuthProblem(
                    403,
                    "CERTIFICATE_AUTH_REQUIRED",
                    "Application callers must authenticate with a certificate",
                )
            if required_permission_name not in roles:
                raise AuthProblem(403, "ROLE_REQUIRED", f"Required application role: {required_role_value}")
            actor_type = "servicePrincipal"

        return Principal(
            tenant_id=tenant_id,
            actor_id=actor_id,
            client_id=client_id,
            actor_type=actor_type,
            scopes=scopes,
            roles=roles,
        )
