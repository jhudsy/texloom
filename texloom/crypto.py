"""Keys, links, encryption and signing. Must match web/src/crypto.ts exactly."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
from dataclasses import dataclass, field
from urllib.parse import parse_qs, quote, urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


def b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


MEMBER_TAG = 0x4D


def canonical_pub(p: str) -> str | None:
    """The one spelling of a 32-byte public key (base64 decoders accept variants, e.g. a changed
    last character), so a revocation always matches the key signer() reports."""
    if not isinstance(p, str) or len(p) > 64:
        return None
    try:
        raw = b64d(p)
    except (ValueError, binascii.Error):
        return None
    return b64e(raw) if len(raw) == 32 else None


def grant_msg(pub: bytes, name: str) -> bytes:
    return b"grant:" + pub + b":" + name.encode()


def revoke_msg(pub_b64: str) -> bytes:
    return b"revoke:" + b64d(pub_b64)


def _ok(pub: bytes, sig: bytes, msg: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(pub).verify(sig, msg)
        return True
    except (InvalidSignature, ValueError):
        return False


def signer(project: str, msg: bytes, sig: bytes) -> str | None:
    """Who signed msg: "owner", a personal key (base64url), or None if invalid.

    A personal signature is 'M' | pub (32) | owner's grant (64) | name length (1) | name | sig (64);
    see web/src/crypto.ts.
    """
    try:
        root = b64d(project)
    except ValueError:
        return None
    if len(sig) == 64:
        return "owner" if _ok(root, sig, msg) else None
    if len(sig) < 162 or sig[0] != MEMBER_TAG:
        return None
    pub, grant, n = sig[1:33], sig[33:97], sig[97]
    if len(sig) != 98 + n + 64:
        return None
    try:
        name = sig[98:98 + n].decode()
    except UnicodeDecodeError:
        return None
    if not _ok(root, grant, grant_msg(pub, name)) or not _ok(pub, sig[98 + n:], msg):
        return None
    return b64e(pub)


@dataclass
class Keys:
    project: str  # base64url Ed25519 public key == project id
    key: bytes  # 32-byte AES-256-GCM key
    seed: bytes | None  # owner's Ed25519 private seed; None => not the owner
    member: tuple[bytes, bytes, str] | None = None  # personal link: (seed, owner's grant, name)
    revoked: set[str] = field(default_factory=set)  # personal keys the owner revoked

    @classmethod
    def generate(cls) -> "Keys":
        sk = Ed25519PrivateKey.generate()
        seed = sk.private_bytes_raw()
        pub = sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return cls(b64e(pub), os.urandom(32), seed)

    @property
    def writable(self) -> bool:
        return self.seed is not None or self.member is not None

    @property
    def member_pub(self) -> str | None:
        if not self.member:
            return None
        pub = Ed25519PrivateKey.from_private_bytes(self.member[0]).public_key()
        return b64e(pub.public_bytes(Encoding.Raw, PublicFormat.Raw))

    def personal(self, name: str) -> tuple[str, str, "Keys"]:
        """Owner only: a personal edit link for `name` (as Keys.personal in web/src/crypto.ts):
        returns (public key, owner's grant, the link's Keys)."""
        if not self.seed:
            raise PermissionError("only the owner can create personal links")
        clean = name.strip()[:60] or "collaborator"
        if len(clean.encode()) > 200:
            raise ValueError("name too long")
        sk = Ed25519PrivateKey.generate()
        mseed = sk.private_bytes_raw()
        pub = sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        grant = Ed25519PrivateKey.from_private_bytes(self.seed).sign(grant_msg(pub, clean))
        return b64e(pub), b64e(grant), Keys(self.project, self.key, None, member=(mseed, grant, clean))

    # --- links -----------------------------------------------------------
    def link(self, server: str, write: bool = True) -> str:
        frag = f"p={self.project}&k={b64e(self.key)}"
        if write and self.seed:
            frag += f"&w={b64e(self.seed)}"
        elif write and self.member:
            frag += f"&m={b64e(self.member[0])}&g={b64e(self.member[1])}&n={quote(self.member[2], safe='')}"
        return f"{server.rstrip('/')}/#{frag}"

    @classmethod
    def from_link(cls, link: str) -> tuple["Keys", str]:
        u = urlsplit(link)
        q = {k: v[0] for k, v in parse_qs(u.fragment).items()}
        server = f"{u.scheme}://{u.netloc}"
        member = None
        if "m" in q and "g" in q and "n" in q:
            mseed, grant, name = b64d(q["m"]), b64d(q["g"]), q["n"]
            mpub = Ed25519PrivateKey.from_private_bytes(mseed).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
            if not _ok(b64d(q["p"]), grant, grant_msg(mpub, name)):
                raise ValueError("this personal link was not issued for this project")
            member = (mseed, grant, name)
        keys = cls(q["p"], b64d(q["k"]), b64d(q["w"]) if "w" in q else None, member)
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
        if self.seed is not None:
            return Ed25519PrivateKey.from_private_bytes(self.seed).sign(msg)
        if self.member is None:
            raise PermissionError("read-only link: cannot sign")
        mseed, grant, name = self.member
        sk = Ed25519PrivateKey.from_private_bytes(mseed)
        nb = name.encode()
        pub = sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return bytes([MEMBER_TAG]) + pub + grant + bytes([len(nb)]) + nb + sk.sign(msg)

    def verify(self, msg: bytes, sig: bytes, fresh: bool = False) -> bool:
        """Signed by the owner or a personal key; `fresh` also refuses revoked keys."""
        who = signer(self.project, msg, sig)
        return who is not None and not (fresh and who in self.revoked)

    @property
    def code(self) -> str:
        """Short project code shown in the web app, e.g. "K7P2-QX4M" (must match projects.ts)."""
        alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
        n = int.from_bytes(b64d(self.project)[:5], "big")  # 40 bits -> 8 characters
        s = "".join(alphabet[(n >> (35 - 5 * i)) & 31] for i in range(8))
        return f"{s[:4]}-{s[4:]}"

    def blob_id(self, plaintext: bytes) -> str:
        return hmac.new(self.key, plaintext, hashlib.sha256).hexdigest()[:32]
