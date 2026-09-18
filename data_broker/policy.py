"""The wall: cross-provider policy semantics for the data broker (S4-2).

The core owns the EVALUATION MECHANICS (tier classification against a
registered table, component-prefix containment, the check() skeleton's
fail-closed stage order - normative, suite invariant 1). This module
owns the data broker's SEMANTICS: the declared-operation vocabulary,
the backend set, and the cross-provider resource normalizer, registered
at boot into a core PolicyRegistry.

The ``resource + operation'' generalization, landed shape (goal
contract sections 4 and 6): a resource is a normalized store node - a
path-like component tuple produced by normalize_store_node() - and an
op is a member of DECLARED_OPERATIONS (read, write, list, move,
trash, mkdir), never a bare mode.

Path-checker rules re-used from the nextcloud broker (GPL-3.0-or-
later; the wall's inheritance per the goal contract section 2):

- exact component-prefix containment (the core's covers() at the
  component level; 'Knowledge' covers 'Knowledge/2026' but never
  'Knowledgeable');
- recursive directory grants (a folder grant covers descendants -
  the same containment rule);
- write-implies-read inside the granted scope (core mechanics);
- escape normalization: percent-decoding is bounded
  (MAX_DECODE_PASSES rounds); a decoded resource is re-normalized;
- traversal ('..' components) refuses (ESCAPES_NAMESPACE);
- fail closed on malformed input: NUL/control characters, empty or
  dotted components, oversized resources (MALFORMED_RESOURCE /
  NORMALIZATION_FAILURE); the wall never raises.

Cross-provider normalization:

- webdav: POSIX-style paths, '/' separators, leading '/' optional;
- onedrive: Graph driveItem paths; a leading '/drives/<id>/root:'
  prefix is stripped so the wall judges the node path, not the API
  envelope shape;

Tier model (the free-lane invariant RETURNS in this domain - a
file/PIM-class domain per the goal contract section 9, unlike the
communications broker's deliberate deviation): 'read' and 'list' are
READ-class (T0/T1, baseline-eligible); 'write', 'move', 'trash',
'mkdir' are GATED (T2, grant required per operation). Undeclared
operations classify GATED (fail closed).
"""

# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from access_broker_core.policy import (
    MAX_DECODE_PASSES,
    MAX_RESOURCE_LENGTH,
    Decision,
    GrantItem,
    Reason,
    Request,
    covers,
)
from access_broker_core.policy import (
    PolicyRegistry as CorePolicyRegistry,
)

__all__ = [
    "DECLARED_OPERATIONS",
    "DeclaredOperation",
    "OperationTier",
    "PolicyError",
    "PolicyRegistry",
    "Reason",
    "build_registry",
    "check",
    "covers",
    "normalize_store_node",
    "tier_table",
]

#: The declared-operation vocabulary (goal contract section 4): the
#: registered operation table is the validation set for grant items
#: AND baseline definitions (the S4-1 generalization's landed shape).
DECLARED_OPERATIONS = frozenset({"read", "write", "list", "move", "trash", "mkdir"})

#: The backend families (goal contract section 3): the generic WebDAV
#: reference implementation plus OneDrive Graph drives. Distinct token
#: scope from the groupware broker's Graph mail/calendar family.
DECLARED_BACKENDS = frozenset({"webdav", "onedrive"})

#: Declared operation names (type alias for signatures and docs).
DeclaredOperation = str

#: The core's two-class enum re-exported under the wall's vocabulary:
#: the data broker's tier table is expressed over it, and the core
#: check() skeleton compares identity against THIS module's enum, so
#: the registry seam normalizes it once at construction.
from access_broker_core.policy import OperationClass as OperationTier  # noqa: E402

_MAX_DECODE_PASSES = MAX_DECODE_PASSES


class PolicyError(Exception):
    """Internal normalization failure carrying its fixed deny Reason.

    The core seam contract is the reason VALUE: check() matches it
    against the core Reason enum by value and denies - it never
    propagates a hostile input as an exception.
    """

    def __init__(self, reason: Reason) -> None:
        self.reason = reason
        super().__init__(reason.value)


def _decode_once(text: str) -> str:
    """One percent-decode round (malformed escapes pass through)."""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "%" and i + 2 < n:
            hexpart = text[i + 1 : i + 3]
            try:
                out.append(chr(int(hexpart, 16)))
                i += 3
                continue
            except ValueError:
                pass
        out.append(ch)
        i += 1
    return "".join(out)


def normalize_store_node(raw: Any, backend: str) -> tuple[str, ...]:
    """The wall's normalizer: raw resource -> path component tuple.

    Cross-provider: webdav and onedrive share the '/'-separated node
    path shape; onedrive's Graph prefix is stripped first. Fail closed
    on every malformed input with a fixed Reason; never raises anything
    but PolicyError; never returns an empty tuple (an empty normalized
    scope would cover the whole account namespace - never grantable).
    """
    if not isinstance(raw, str):
        raise PolicyError(Reason.MALFORMED_RESOURCE)
    if backend not in DECLARED_BACKENDS:
        raise PolicyError(Reason.UNKNOWN_BACKEND)
    if len(raw) > MAX_RESOURCE_LENGTH:
        raise PolicyError(Reason.MALFORMED_RESOURCE)
    text = raw
    # Bounded percent-decoding: the decoded resource is re-normalized,
    # so encoded separators ('%2F') cannot smuggle components past the
    # splitting stage.
    for _ in range(_MAX_DECODE_PASSES):
        if "%" not in text:
            break
        new = _decode_once(text)
        if new == text:
            break
        text = new
    else:
        raise PolicyError(Reason.NORMALIZATION_FAILURE)
    if "\x00" in text:
        raise PolicyError(Reason.MALFORMED_RESOURCE)
    control_char_floor = 32  # C0 controls (NUL handled separately above)
    if any(ord(ch) < control_char_floor and ch not in "\t\n" for ch in text):
        raise PolicyError(Reason.MALFORMED_RESOURCE)
    if backend == "onedrive":
        text = _strip_graph_prefix(text)
    if text in ("", "/"):
        raise PolicyError(Reason.EMPTY_RESOURCE)
    text = text.lstrip("/")
    components = text.split("/")
    if any(c in ("", ".") for c in components):
        raise PolicyError(Reason.MALFORMED_RESOURCE)
    if any(c == ".." for c in components):
        raise PolicyError(Reason.ESCAPES_NAMESPACE)
    return tuple(components)


def _strip_graph_prefix(text: str) -> str:
    """Strip onedrive's '/drives/<id>/root:' API shape, keeping the node
    path the wall judges."""
    if text.startswith("/drives/") and "/root:" in text:
        text = text.split("/root:", 1)[1]
    return text


def tier_table() -> dict[str, OperationTier]:
    """The data broker's tier table (goal contract section 4).

    Reads and lists are READ-class (the free lane RETURNS in this
    file-class domain); the declared writes are GATED per operation.
    """
    free = {"read", "list"}
    return {
        op: (OperationTier.READ if op in free else OperationTier.GATED)
        for op in sorted(DECLARED_OPERATIONS)
    }


class PolicyRegistry(CorePolicyRegistry):
    """The data broker's registry (the core seam, data-broker semantics)."""


def build_registry(
    backends: frozenset[str] | set[str] | None = None,
) -> PolicyRegistry:
    """Construct the registry the core mechanics evaluate against."""
    table = tier_table()
    return PolicyRegistry(
        operation_class=table,
        backends=backends if backends is not None else set(DECLARED_BACKENDS),
        normalize_resource=normalize_store_node,
    )


def check(
    registry: PolicyRegistry,
    grant_item: GrantItem,
    request_item: Request,
    clock: Callable[[], datetime],
) -> Decision:
    """The wall: core check() skeleton with the data-broker registry.

    The clock is INJECTED (suite invariant: nothing in the wall reads
    wall-clock time). Never raises; every malformed input denies with a
    fixed Reason. Thin alias so the wall's call sites name the broker
    module (the core owns the normative stage order).
    """
    return _core_check(registry, grant_item, request_item, clock)


# Re-export the core check under a private alias (the skeleton).
from access_broker_core.policy import check as _core_check  # noqa: E402
