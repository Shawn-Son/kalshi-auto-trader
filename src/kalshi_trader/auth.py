from __future__ import annotations

import base64
import time
from pathlib import Path
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


class AuthenticationError(RuntimeError):
    pass


class RequestSigner:
    def __init__(self, api_key_id: str, private_key_path: Path) -> None:
        try:
            material = private_key_path.read_bytes()
            key = serialization.load_pem_private_key(material, password=None)
        except (OSError, ValueError, TypeError) as exc:
            raise AuthenticationError(f"cannot load RSA private key: {exc}") from exc
        if not isinstance(key, rsa.RSAPrivateKey):
            raise AuthenticationError("Kalshi private key must be an RSA private key")
        self._api_key_id = api_key_id
        self._private_key = key

    def headers(
        self,
        method: str,
        url: str,
        *,
        timestamp_ms: int | None = None,
    ) -> dict[str, str]:
        timestamp = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
        path = urlparse(url).path
        message = f"{timestamp}{method.upper()}{path}".encode()
        signature = self._private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self._api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("ascii"),
        }
