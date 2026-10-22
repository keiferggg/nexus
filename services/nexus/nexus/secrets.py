"""Credential storage. Records hold only opaque references ("secret:<uuid>"); the value lives in
the OS credential store (Windows Credential Manager / macOS Keychain / Secret Service) via
``keyring``. Values are read only inside the backend process at call time and are never
returned by the API, written to logs, placed in prompts, or exported in backups."""

from __future__ import annotations

import os
import uuid

SERVICE = "NEXUS"


class SecretStore:
    backend_name = "os-keyring"

    def __init__(self) -> None:
        import keyring  # imported lazily so tests can choose the memory backend

        self._keyring = keyring
        self.backend_name = f"os-keyring:{type(keyring.get_keyring()).__name__}"

    def put(self, value: str) -> str:
        ref = f"secret:{uuid.uuid4().hex}"
        self._keyring.set_password(SERVICE, ref, value)
        return ref

    def get(self, ref: str | None) -> str | None:
        if not ref:
            return None
        return self._keyring.get_password(SERVICE, ref)

    def delete(self, ref: str | None) -> None:
        if not ref:
            return
        try:
            self._keyring.delete_password(SERVICE, ref)
        except Exception:
            pass

    _probe_result: tuple[bool, str] | None = None

    def available(self) -> tuple[bool, str]:
        if self._probe_result is not None:
            return self._probe_result
        self._probe_result = self._probe()
        return self._probe_result

    def _probe(self) -> tuple[bool, str]:
        try:
            probe = f"probe:{uuid.uuid4().hex}"
            self._keyring.set_password(SERVICE, probe, "x")
            ok = self._keyring.get_password(SERVICE, probe) == "x"
            self._keyring.delete_password(SERVICE, probe)
            return ok, self.backend_name
        except Exception as exc:  # pragma: no cover - platform dependent
            return False, f"{self.backend_name}: {type(exc).__name__}"


class MemorySecretStore(SecretStore):
    """TEST/DEMO ONLY. Process-memory credentials; lost on restart. Selected explicitly via
    NEXUS_SECRET_BACKEND=memory and reported as such in diagnostics."""

    backend_name = "memory (test only - not persistent)"

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def put(self, value: str) -> str:
        ref = f"secret:{uuid.uuid4().hex}"
        self._data[ref] = value
        return ref

    def get(self, ref: str | None) -> str | None:
        return self._data.get(ref) if ref else None

    def delete(self, ref: str | None) -> None:
        if ref:
            self._data.pop(ref, None)

    def available(self) -> tuple[bool, str]:
        return True, self.backend_name


def make_secret_store() -> SecretStore:
    if os.environ.get("NEXUS_SECRET_BACKEND") == "memory":
        return MemorySecretStore()
    return SecretStore()


def redact(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s and len(s) >= 6:
            text = text.replace(s, "[REDACTED]")
    return text
