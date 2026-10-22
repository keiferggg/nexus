"""Real OS credential store (Windows Credential Manager via keyring). Opt-in: NEXUS_REAL_KEYRING=1.
Writes dummy entries under the service name "NEXUS" and deletes them; asserts nothing is left behind."""
import os
import sqlite3

import keyring
import pytest

from nexus.appctx import build_context
from nexus.secrets import SERVICE, SecretStore
from nexus.tools.sandbox import UnavailableSandbox

pytestmark = pytest.mark.skipif(os.environ.get("NEXUS_REAL_KEYRING") != "1", reason="set NEXUS_REAL_KEYRING=1")
DUMMY = "sk-NEXUS-TEST-DUMMY-not-a-real-key-0123456789"


def test_real_credential_store_round_trip_and_cleanup(tmp_path):
    store = SecretStore()
    ok, backend = store.available()
    assert ok, backend
    assert "WinVault" in backend or os.name != "nt", backend
    c = build_context(tmp_path / "d", secrets=store, sandbox=UnavailableSandbox("test"))
    try:
        pid = c.registry.add(kind="openai", label="dummy", api_key=DUMMY)
        ref = c.db.one("SELECT secret_ref FROM providers WHERE id = ?", (pid,))["secret_ref"]
        assert ref.startswith("secret:") and keyring.get_password(SERVICE, ref) == DUMMY
        new_ref_key = DUMMY.replace("DUMMY", "DUMMY2")
        c.registry.replace_key(pid, new_ref_key)
        ref2 = c.db.one("SELECT secret_ref FROM providers WHERE id = ?", (pid,))["secret_ref"]
        assert keyring.get_password(SERVICE, ref) is None, "old key deleted on replacement"
        assert keyring.get_password(SERVICE, ref2) == new_ref_key
        c.registry.remove(pid)
        assert keyring.get_password(SERVICE, ref2) is None, "key deleted with the provider"
    finally:
        c.close()
    raw = (tmp_path / "d" / "nexus.db").read_bytes()
    wal = tmp_path / "d" / "nexus.db-wal"
    raw += wal.read_bytes() if wal.exists() else b""
    assert b"NEXUS-TEST-DUMMY" not in raw, "the key never touches the database"
