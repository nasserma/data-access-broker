"""S4-2: the cross-provider policy wall (TDD-first battery).

The wall owns the data broker's SEMANTICS (goal contract section 4);
the core owns the MECHANICS. Scope object: (backend, account,
resource, op) with the ``resource + operation'' generalization: a
resource is a normalized store node (path-like component tuple) and an
op is a declared operation (read, write, list, move, trash, mkdir),
not a bare mode.

Rules re-used from the nextcloud broker (the wall's inheritance):

- exact component-prefix match, never partial components;
- escape normalization: percent-decoding bounded, traversal refused;
- fail closed on malformed input, always;
- recursive directory grants (a folder grant covers its descendants);
- write-implies-read inside the granted scope (core mechanics);
- evaluated tier-first through the core check() skeleton (normative
  stage order, suite invariant 1).

Cross-provider normalization (this stage's redesign):

- webdav: POSIX-style paths, '/' separators, leading '/' optional;
  percent-decoding is bounded (MAX_DECODE_PASSES) and a decoded
  resource is re-normalized; '..' components refuse (traversal);
- onedrive: Graph driveItem paths, '/' separators, leading
  '/drives/<id>/root:' prefixes are stripped (the wall judges the
  node path, not the API shape);
- empty normalized scopes refuse wholesale (the wall never grants a
  whole account namespace);
- hostile inputs (NULs, control characters, oversized resources)
  refuse with MALFORMED_RESOURCE.

Fuzz: hypothesis strategies drive the normalizer on arbitrary bytes
(the goal contract's ``hypothesis fuzz on the normalizer and any
parser, unmockable'').
"""

# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from data_broker import policy
from data_broker.policy import (
    OperationTier,
    PolicyError,
    normalize_store_node,
)

T0 = datetime(2026, 9, 13, 12, 0, 0)
def _clock() -> datetime:
    return T0  # injected clock, never real time


def make_registry(**kwargs: Any) -> policy.PolicyRegistry:
    return policy.build_registry(**kwargs)


def grant(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "backend": "webdav",
        "account": "personal",
        "resource": "Knowledge",
        "ops": ["read"],
        "expires_at": T0 + timedelta(hours=24),
    }
    base.update(overrides)
    return base


def request(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "backend": "webdav",
        "account": "personal",
        "resource": "Knowledge",
        "op": "read",
    }
    base.update(overrides)
    return base


# ------------------------------------------------------------------ normalizer


def test_plain_path_normalizes_to_components() -> None:
    assert normalize_store_node("Knowledge/2026/paper.tex", "webdav") == (
        "Knowledge",
        "2026",
        "paper.tex",
    )


def test_leading_slash_optional() -> None:
    assert normalize_store_node("/Knowledge", "webdav") == ("Knowledge",)


def test_root_refuses() -> None:
    """An empty normalized scope would cover the whole namespace."""
    with pytest.raises(PolicyError):
        normalize_store_node("/", "webdav")
    with pytest.raises(PolicyError):
        normalize_store_node("", "webdav")


def test_traversal_refused() -> None:
    with pytest.raises(PolicyError) as excinfo:
        normalize_store_node("Knowledge/../Secrets", "webdav")
    assert excinfo.value.reason == policy.Reason.ESCAPES_NAMESPACE


def test_double_traversal_refused() -> None:
    with pytest.raises(PolicyError):
        normalize_store_node("../../etc", "webdav")


def test_nested_traversal_refused() -> None:
    with pytest.raises(PolicyError):
        normalize_store_node("a/b/../../../c", "webdav")


def test_percent_encoding_decoded_bounded() -> None:
    assert normalize_store_node("My%20Documents/notes", "webdav") == (
        "My Documents",
        "notes",
    )


def test_percent_runaway_refused() -> None:
    """More than MAX_DECODE_PASSES decode rounds is hostile input."""
    hostile = "a" + "%2525252520" * 5 + "b"
    with pytest.raises(PolicyError) as excinfo:
        normalize_store_node(hostile, "webdav")
    assert excinfo.value.reason == policy.Reason.NORMALIZATION_FAILURE


def test_oversized_resource_refused() -> None:
    with pytest.raises(PolicyError) as excinfo:
        normalize_store_node("a" * (policy.MAX_RESOURCE_LENGTH + 1), "webdav")
    assert excinfo.value.reason == policy.Reason.MALFORMED_RESOURCE


def test_control_characters_refused() -> None:
    with pytest.raises(PolicyError):
        normalize_store_node("bad\x00name", "webdav")


def test_empty_component_refused() -> None:
    with pytest.raises(PolicyError):
        normalize_store_node("Knowledge//notes", "webdav")


def test_dot_component_refused() -> None:
    with pytest.raises(PolicyError):
        normalize_store_node("Knowledge/./notes", "webdav")


def test_onedrive_graph_prefix_stripped() -> None:
    assert normalize_store_node(
        "/drives/b!xyz/root:/Work/Reports", "onedrive"
    ) == ("Work", "Reports")


def test_onedrive_plain_path() -> None:
    assert normalize_store_node("Work/Reports", "onedrive") == ("Work", "Reports")


# ------------------------------------------------------------------- fuzz


@settings(max_examples=300, deadline=None)
@given(st.binary(min_size=0, max_size=200))
def test_fuzz_normalizer_never_crashes(raw: bytes) -> None:
    """The normalizer either returns components or raises PolicyError
    with a fixed reason - never anything else, never a non-PolicyError."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return  # non-UTF8 bytes are refused at the adapter boundary
    try:
        components = normalize_store_node(text, "webdav")
    except PolicyError:
        return  # a fixed-reason refusal is the contract
    assert all(isinstance(c, str) and c for c in components)
    assert ".." not in components


@settings(max_examples=200, deadline=None)
@given(st.text(alphabet="/%.aA \t\x00", min_size=0, max_size=100))
def test_fuzz_hostile_alphabet(text: str) -> None:
    """Hostile alphabet: escapes, separators, dots, nulls, whitespace."""
    try:
        components = normalize_store_node(text, "webdav")
    except PolicyError:
        return
    assert ".." not in components
    assert all("/" not in c and "\x00" not in c for c in components)


# ------------------------------------------------------------------ tier table


def test_declared_operation_set() -> None:
    assert frozenset(
        {"read", "write", "list", "move", "trash", "mkdir"}
    ) == policy.DECLARED_OPERATIONS


def test_tier_classification() -> None:
    """Free lane RETURNS here (file/PIM-class domain, T0/T1 reads free
    per the Sep 8 audit tier model): reads and lists are READ-class;
    writes, moves, trash, mkdir are GATED."""
    registry = policy.build_registry()
    assert registry.classify("read") is OperationTier.READ
    assert registry.classify("list") is OperationTier.READ
    assert registry.classify("write") is OperationTier.GATED
    assert registry.classify("move") is OperationTier.GATED
    assert registry.classify("trash") is OperationTier.GATED
    assert registry.classify("mkdir") is OperationTier.GATED


def test_undeclared_operation_fails_closed() -> None:
    registry = policy.build_registry()
    assert registry.classify("format") is OperationTier.GATED
    assert registry.classify("DELETE") is OperationTier.GATED


def test_registry_refuses_empty_backends() -> None:
    from access_broker_core.policy import OperationClass

    registry = policy.PolicyRegistry(
        operation_class={"read": OperationClass.READ},
        backends=frozenset(),
        normalize_resource=normalize_store_node,
    )
    with pytest.raises(ValueError):
        registry.validate()


# ------------------------------------------------------------------ the wall


def test_grant_covers_descendants() -> None:
    registry = policy.build_registry()
    decision = policy.check(registry, grant(), request(resource="Knowledge/2026/notes.txt"), clock=_clock)
    assert decision.allowed
    assert decision.reason == policy.Reason.OK


def test_grant_does_not_cover_partial_components() -> None:
    registry = policy.build_registry()
    decision = policy.check(registry, grant(), request(resource="Knowledgeable"), clock=_clock)
    assert not decision.allowed
    assert decision.reason == policy.Reason.NOT_IN_SCOPE


def test_op_mismatch_denied() -> None:
    registry = policy.build_registry()
    decision = policy.check(registry, grant(ops=["read"]), request(op="write"), clock=_clock)
    assert not decision.allowed
    assert decision.reason == policy.Reason.OP_NOT_GRANTED


def test_write_implies_read() -> None:
    registry = policy.build_registry()
    decision = policy.check(registry, grant(ops=["write"]), request(op="read"), clock=_clock)
    assert decision.allowed


def test_write_does_not_imply_write() -> None:
    registry = policy.build_registry()
    decision = policy.check(registry, grant(ops=["mkdir"]), request(op="write"), clock=_clock)
    assert not decision.allowed


def test_expired_grant_denied() -> None:
    registry = policy.build_registry()
    decision = policy.check(
        registry,
        grant(expires_at=T0 - timedelta(hours=1)),
        request(),
        clock=_clock,
    )
    assert not decision.allowed
    assert decision.reason == policy.Reason.EXPIRED


def test_account_mismatch_denied() -> None:
    registry = policy.build_registry()
    decision = policy.check(registry, grant(), request(account="continuum"), clock=_clock)
    assert not decision.allowed
    assert decision.reason == policy.Reason.ACCOUNT_MISMATCH


def test_backend_mismatch_denied() -> None:
    registry = policy.build_registry()
    decision = policy.check(registry, grant(), request(backend="onedrive"), clock=_clock)
    assert not decision.allowed
    assert decision.reason == policy.Reason.BACKEND_MISMATCH


def test_malformed_request_op_denied() -> None:
    registry = policy.build_registry()
    decision = policy.check(registry, grant(), request(op=123), clock=_clock)  # type: ignore[arg-type]
    assert not decision.allowed


def test_malformed_resource_denied() -> None:
    registry = policy.build_registry()
    decision = policy.check(registry, grant(), request(resource="a\x00b"), clock=_clock)
    assert not decision.allowed


def test_normalization_failure_denied_not_raised() -> None:
    """The wall never raises: hostile resources deny with a fixed reason."""
    registry = policy.build_registry()
    decision = policy.check(registry, grant(), request(resource="x%25%25%25%25%25%2520y"), clock=_clock)
    assert not decision.allowed


def test_wall_fuzz_grant_request_pairs() -> None:
    """Fuzzed request resources against a fixed grant: any allowed
    decision must be justified by component containment."""
    registry = policy.build_registry()

    @settings(max_examples=150, deadline=None)
    @given(st.text(alphabet="ab/%.~ \t", min_size=0, max_size=40))
    def run(text: str) -> None:
        try:
            decision = policy.check(registry, grant(resource="a"), request(resource=text), clock=_clock)
        except Exception:  # noqa: BLE001 - the wall must not raise, ever
            pytest.fail("check() raised on hostile input")
        if decision.allowed:
            components = normalize_store_node(text, "webdav")
            assert components[:1] == ("a",)

    run()
