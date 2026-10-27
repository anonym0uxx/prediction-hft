"""Kalshi API auth (Ed25519). Key ID is config; the private key PEM lives ONLY in Windows Credential Manager
(target predhft:kalshi:ed25519_private_pem) and is read into memory at process start — never from a file, never logged."""
from __future__ import annotations
import base64, ctypes, ctypes.wintypes as wt, os, time
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

KEY_ID = os.environ.get("KALSHI_KEY_ID", "9dc6a82d-0442-494e-b862-a59a16f5a354")   # public identifier, not a secret
CRED_TARGET = "predhft:kalshi:ed25519_private_pem"

class _CRED(ctypes.Structure):
    _fields_ = [("Flags", wt.DWORD), ("Type", wt.DWORD), ("TargetName", wt.LPWSTR), ("Comment", wt.LPWSTR), ("LastWritten", wt.FILETIME),
                ("CredentialBlobSize", wt.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_char)), ("Persist", wt.DWORD), ("AttributeCount", wt.DWORD),
                ("Attributes", ctypes.c_void_p), ("TargetAlias", wt.LPWSTR), ("UserName", wt.LPWSTR)]

def _read_credman(target: str) -> bytes:
    p = ctypes.POINTER(_CRED)(); adv = ctypes.windll.advapi32
    if not adv.CredReadW(target, 1, 0, ctypes.byref(p)): raise RuntimeError(f"credential {target} not found")
    try: return ctypes.string_at(p.contents.CredentialBlob, p.contents.CredentialBlobSize)
    finally: adv.CredFree(p)

_KEY: Ed25519PrivateKey | None = None
def private_key() -> Ed25519PrivateKey:
    global _KEY
    if _KEY is None:
        k = serialization.load_pem_private_key(_read_credman(CRED_TARGET), password=None)
        if not isinstance(k, Ed25519PrivateKey): raise TypeError("stored key is not Ed25519")
        _KEY = k
    return _KEY

def headers(method: str, path: str, ts_ms: int | None = None) -> dict:
    """KALSHI-ACCESS-* headers: signature = Ed25519(timestamp_ms + METHOD + path), base64."""
    ts = str(ts_ms or int(time.time() * 1000)); sig = private_key().sign((ts + method.upper() + path).encode())
    return {"KALSHI-ACCESS-KEY": KEY_ID, "KALSHI-ACCESS-TIMESTAMP": ts, "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}
