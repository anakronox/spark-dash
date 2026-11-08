"""API keys sealed with the agents' shared token (roadmap CU6).

WHY. The agent fetches its config from the backend over plain HTTP. Sending the
token as a bearer and getting the keys back in clear put both on the LAN for
anyone reading traffic between a node and the VM. Sealed, neither travels:
the agent asks without the token, and the backend answers with the keys
encrypted under a key derived from it. Only a holder of the token can open
them, and a changed byte fails to open rather than yielding a wrong key.

AES-256-GCM, its key from HKDF-SHA256 over the token. The node's id is the
associated data, so a blob sealed for one node does not open as another's.
"""

from __future__ import annotations

import base64
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

VERSION = 1
_INFO = b"spark-dash agent keys v1"


class SealError(ValueError):
    """The blob did not open: a different token, another node's, or altered."""


def _key(token: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_INFO).derive(
        token.encode()
    )


def seal(token: str, node: str, keys: dict[str, str]) -> dict:
    nonce = os.urandom(12)
    data = AESGCM(_key(token)).encrypt(nonce, json.dumps(keys).encode(), node.encode())
    return {
        "v": VERSION,
        "nonce": base64.b64encode(nonce).decode(),
        "data": base64.b64encode(data).decode(),
    }


def open_sealed(token: str, node: str, blob: object) -> dict[str, str]:
    try:
        if not isinstance(blob, dict) or blob.get("v") != VERSION:
            raise SealError("not a sealed key set this agent knows")
        nonce = base64.b64decode(blob["nonce"], validate=True)
        data = base64.b64decode(blob["data"], validate=True)
        plain = AESGCM(_key(token)).decrypt(nonce, data, node.encode())
        keys = json.loads(plain)
    except SealError:
        raise
    except (InvalidTag, KeyError, ValueError, TypeError) as exc:
        raise SealError("the sealed keys did not open with this token") from exc
    if not isinstance(keys, dict):
        raise SealError("the sealed keys are not a mapping")
    return {str(u): str(k) for u, k in keys.items() if isinstance(k, str) and k}
