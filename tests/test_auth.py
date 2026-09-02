from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from media_manager.app import create_app
from media_manager.auth import CloudflareAccessVerifier
from media_manager.config import AuthMode, Settings
from media_manager.errors import ApiError

ISSUER = "https://example.cloudflareaccess.com"
AUDIENCE = "test-access-audience"


class StartupOnlyProcessor:
    async def verify(self) -> None:
        return None

    async def convert(self, *_args, **_kwargs):
        raise AssertionError("Conversion must not run")


def cloudflare_settings(tmp_path: Path) -> Settings:
    return Settings(
        work_dir=tmp_path / "work",
        auth_mode=AuthMode.CLOUDFLARE,
        public_base_url="https://media.example.test",
        cloudflare_issuer=ISSUER,
        cloudflare_audience=AUDIENCE,
    )


@pytest.mark.asyncio
async def test_cloudflare_mode_rejects_a_missing_assertion_but_keeps_health_local(
    tmp_path,
) -> None:
    app = create_app(cloudflare_settings(tmp_path), processor=StartupOnlyProcessor())
    transport = httpx.ASGITransport(app=app)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as client,
    ):
        assert (await client.get("/health/ready")).status_code == 200
        web_response = await client.get("/")
        response = await client.get("/v1/capabilities")

    assert web_response.status_code == 401
    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "code": "ACCESS_TOKEN_REQUIRED",
            "message": "Cloudflare Access token is required",
        }
    }


@pytest.mark.asyncio
async def test_cloudflare_verifier_accepts_a_signed_service_principal(monkeypatch) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = CloudflareAccessVerifier(ISSUER, AUDIENCE)
    monkeypatch.setattr(
        verifier._jwks,
        "get_signing_key_from_jwt",
        lambda _token: SimpleNamespace(key=private_key.public_key()),
    )
    token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": [AUDIENCE],
            "exp": datetime.now(UTC) + timedelta(minutes=5),
            "sub": "",
            "common_name": "ios-test-device",
        },
        private_key,
        algorithm="RS256",
    )

    principal = await verifier.verify(token)

    assert principal.id == "service:ios-test-device"


@pytest.mark.asyncio
async def test_cloudflare_verifier_rejects_the_wrong_audience(monkeypatch) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = CloudflareAccessVerifier(ISSUER, AUDIENCE)
    monkeypatch.setattr(
        verifier._jwks,
        "get_signing_key_from_jwt",
        lambda _token: SimpleNamespace(key=private_key.public_key()),
    )
    token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": ["another-application"],
            "exp": datetime.now(UTC) + timedelta(minutes=5),
            "sub": "user-id",
        },
        private_key,
        algorithm="RS256",
    )

    with pytest.raises(ApiError) as error:
        await verifier.verify(token)

    assert error.value.status_code == 401
    assert error.value.code == "INVALID_ACCESS_TOKEN"


@pytest.mark.parametrize(
    "issuer",
    [
        "http://example.cloudflareaccess.com",
        "https://example.invalid",
        "https://user@example.cloudflareaccess.com",
        "https://example.cloudflareaccess.com/path",
    ],
)
def test_cloudflare_issuer_configuration_is_restricted(issuer: str) -> None:
    with pytest.raises(ValueError, match="CF_ISSUER"):
        Settings(
            auth_mode=AuthMode.CLOUDFLARE,
            public_base_url="https://media.example.test",
            cloudflare_issuer=issuer,
            cloudflare_audience=AUDIENCE,
        )
