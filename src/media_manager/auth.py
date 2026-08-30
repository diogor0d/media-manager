from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import Request

from media_manager.config import AuthMode, Settings
from media_manager.errors import ApiError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Principal:
    id: str


class CloudflareAccessVerifier:
    def __init__(self, issuer: str, audience: str) -> None:
        self._issuer = issuer.rstrip("/")
        self._audience = audience
        self._jwks = jwt.PyJWKClient(
            f"{self._issuer}/cdn-cgi/access/certs",
            cache_keys=True,
            lifespan=300,
            timeout=5,
        )

    async def verify(self, token: str) -> Principal:
        try:
            signing_key = await asyncio.to_thread(self._jwks.get_signing_key_from_jwt, token)
            claims: dict[str, Any] = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iss", "aud"]},
            )
        except Exception as exc:
            logger.warning("Cloudflare Access JWT validation failed: %s", type(exc).__name__)
            raise ApiError(401, "INVALID_ACCESS_TOKEN", "Access token is invalid") from exc

        subject = claims.get("sub")
        if isinstance(subject, str) and subject:
            return Principal(id=f"user:{subject}")

        common_name = claims.get("common_name")
        if isinstance(common_name, str) and common_name:
            return Principal(id=f"service:{common_name}")

        raise ApiError(401, "INVALID_ACCESS_TOKEN", "Access token has no usable principal")


class Authenticator:
    def __init__(self, settings: Settings) -> None:
        self._mode = settings.auth_mode
        self._verifier: CloudflareAccessVerifier | None = None
        if self._mode is AuthMode.CLOUDFLARE:
            assert settings.cloudflare_issuer is not None
            assert settings.cloudflare_audience is not None
            self._verifier = CloudflareAccessVerifier(
                settings.cloudflare_issuer,
                settings.cloudflare_audience,
            )

    async def authenticate(self, request: Request) -> Principal:
        if self._mode is AuthMode.DISABLED:
            return Principal(id="local-development")

        assertion = request.headers.get("Cf-Access-Jwt-Assertion")
        if not assertion:
            raise ApiError(401, "ACCESS_TOKEN_REQUIRED", "Cloudflare Access token is required")

        assert self._verifier is not None
        return await self._verifier.verify(assertion)


async def get_principal(request: Request) -> Principal:
    authenticator: Authenticator = request.app.state.authenticator
    return await authenticator.authenticate(request)
