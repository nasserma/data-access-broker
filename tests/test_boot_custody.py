"""S6-2 boot-path custody batteries (data broker).

The custody layer's deployment gates, run through the REAL boot with a
REAL config: the registry is built and refuse-to-start validated at
boot, and the audit chain carries the custody class of the backend
principal on executed operations.
"""

# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import json
from pathlib import Path

from access_broker_core.audit import verify_chain
from access_broker_core.custody import CustodyClass, CustodyRegistry
from test_boot_path import booted  # noqa: F401


def test_boot_registers_custody(booted):  # noqa: F811
    """The real boot builds and validates the custody registry with the
    data broker declarations."""
    custody = booted.custody
    assert custody.declared_backends() == ["onedrive", "webdav"]
    assert custody.declared("webdav") is CustodyClass.SCOPED
    assert custody.declared("onedrive") is CustodyClass.SCOPED
    custody.validate()


async def test_boot_audit_carries_custody_class(booted):  # noqa: F811
    """An executed operation's audit entry carries the custody class of
    its backend principal (webdav = scoped), and the chain verifies."""
    ctx = booted

    async def _call():
        return b"hello"

    result = await ctx.execute("scratch", "docs/report.txt", "read", _call)
    assert result["status"] == "ok"

    audit_path = Path(ctx.audit._path)  # noqa: SLF001 - the boot-wired log path
    executed = [
        json.loads(ln)
        for ln in audit_path.read_text().splitlines()
        if '"executed"' in ln
    ]
    assert executed, "the executed read must be in the audit chain"
    assert executed[-1]["custody"] == "scoped"
    assert verify_chain(audit_path).ok


def test_custody_change_refuses():
    registry = CustodyRegistry()
    registry.register("webdav", CustodyClass.SCOPED)
    import pytest

    with pytest.raises(ValueError, match="already declared"):
        registry.register("webdav", CustodyClass.ACCOUNT_WIDE)
