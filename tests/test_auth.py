from __future__ import annotations

import base64
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshi_trader.auth import RequestSigner


def _private_key_file(tmp_path: Path) -> tuple[rsa.RSAPrivateKey, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / "private.key"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return key, path


def test_signature_excludes_query_string(tmp_path: Path) -> None:
    key, path = _private_key_file(tmp_path)
    signer = RequestSigner("key-id", path)
    headers = signer.headers(
        "get",
        "https://example.test/trade-api/v2/portfolio/orders?limit=5",
        timestamp_ms=1_700_000_000_000,
    )
    key.public_key().verify(
        base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
        b"1700000000000GET/trade-api/v2/portfolio/orders",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    assert headers["KALSHI-ACCESS-KEY"] == "key-id"
