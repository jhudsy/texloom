"""Keys, links, encryption and signing. Must match web/src/crypto.ts exactly."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


def b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


@dataclass
class Keys:
    project: str  # base64url Ed25519 public key == project id
    key: bytes  # 32-byte AES-256-GCM key
    seed: bytes | None  # 32-byte Ed25519 private seed; None => read-only

    @classmethod
    def generate(cls) -> "Keys":
        sk = Ed25519PrivateKey.generate()
        seed = sk.private_bytes_raw()
        pub = sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return cls(b64e(pub), os.urandom(32), seed)

    @property
    def writable(self) -> bool:
        return self.seed is not None

    # --- links -----------------------------------------------------------
    def link(self, server: str, write: bool = True) -> str:
        frag = f"p={self.project}&k={b64e(self.key)}"
        if write and self.seed:
            frag += f"&w={b64e(self.seed)}"
        return f"{server.rstrip('/')}/#{frag}"

    @classmethod
    def from_link(cls, link: str) -> tuple["Keys", str]:
        u = urlsplit(link)
        q = {k: v[0] for k, v in parse_qs(u.fragment).items()}
        server = f"{u.scheme}://{u.netloc}"
        keys = cls(q["p"], b64d(q["k"]), b64d(q["w"]) if "w" in q else None)
        if keys.seed is not None:
            sk = Ed25519PrivateKey.from_private_bytes(keys.seed)
            pub = sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
            if b64e(pub) != keys.project:
                raise ValueError("edit key does not match project id")
        return keys, server

    # --- crypto ----------------------------------------------------------
    def encrypt(self, plaintext: bytes) -> bytes:
        iv = os.urandom(12)
        return iv + AESGCM(self.key).encrypt(iv, plaintext, None)

    def decrypt(self, ciphertext: bytes) -> bytes:
        return AESGCM(self.key).decrypt(ciphertext[:12], ciphertext[12:], None)

    def sign(self, msg: bytes) -> bytes:
        if self.seed is None:
            raise PermissionError("read-only link: cannot sign")
        return Ed25519PrivateKey.from_private_bytes(self.seed).sign(msg)

    def blob_id(self, plaintext: bytes) -> str:
        return hmac.new(self.key, plaintext, hashlib.sha256).hexdigest()[:32]
