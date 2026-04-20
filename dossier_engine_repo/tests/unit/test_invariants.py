"""
Tests for `engine.pipeline.invariants.enforce_used_generated_disjoint`.

The phase under test enforces one rule: a logical entity must not
appear in both `used` and `generated` for the same activity. The
rule exists because revising an entity IS using it — the parent
version is implied by `wasDerivedFrom`, so listing the parent in
`used` would duplicate the edge.

"Logical entity" means the `entity_id` for local refs (so v1 and
v2 of the same logical entity are one thing) and the full URI for
externals.

These tests exercise every branch:

* built-in activity exemption (the phase returns early)
* empty used set (the phase returns early)
* local overlap by entity_id across version ids
* external overlap by URI
* mixed local and external in the same overlap
* no overlap when entity_ids differ
* multiple overlaps reported together
"""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from dossier_engine.engine.errors import ActivityError
from dossier_engine.engine.pipeline.invariants import (
    enforce_used_generated_disjoint,
)
from dossier_engine.engine.state import UsedRef


# Fixed UUIDs so assertion failure messages are readable.
E_A = UUID("11111111-1111-1111-1111-111111111111")
E_B = UUID("22222222-2222-2222-2222-222222222222")
V_A1 = UUID("a1111111-1111-1111-1111-111111111111")
V_A2 = UUID("a2222222-2222-2222-2222-222222222222")
V_B1 = UUID("b1111111-1111-1111-1111-111111111111")


def _local_used_ref(entity_id: UUID, version_id: UUID, etype: str = "oe:foo"):
    """What `resolve_used` would put in `state.used_refs` for a local
    entity. `entity` is the full `type/eid@vid` ref."""
    return UsedRef(
        entity=f"{etype}/{entity_id}@{version_id}",
        version_id=version_id,
        type=etype,
    )


def _external_used_ref(uri: str):
    """What `resolve_used` would put in `state.used_refs` for an
    external URI."""
    return UsedRef(
        entity=uri,
        external=True,
        version_id=uuid4(),
    )


def _local_generated_item(entity_id: UUID, version_id: UUID, etype: str = "oe:foo"):
    """Shape: raw client input for a local generated entity —
    this is what `state.generated_items` holds before
    `process_generated` runs."""
    return {
        "entity": f"{etype}/{entity_id}@{version_id}",
        "content": {},
    }


def test_built_in_activity_exempt_even_with_overlap(stub_state):
    """Built-in activities (tombstone, systemAction, etc.) are
    exempt from the disjoint check — they have their own shape
    validators and sometimes need the same logical entity in both
    used and generated (tombstone is the canonical example)."""
    state = stub_state(
        activity_def={"name": "tombstone", "built_in": True},
        used_refs=[_local_used_ref(E_A, V_A1)],
        generated_items=[_local_generated_item(E_A, V_A2)],
    )
    enforce_used_generated_disjoint(state)  # no raise


def test_empty_used_returns_early(stub_state):
    """If nothing was used, nothing can overlap. No raise."""
    state = stub_state(
        activity_def={"name": "neemBeslissing", "built_in": False},
        used_refs=[],
        generated_items=[_local_generated_item(E_A, V_A1)],
    )
    enforce_used_generated_disjoint(state)  # no raise


def test_local_overlap_different_versions_same_entity_id(stub_state):
    """The whole point of the rule: v1 in `used`, v2 in `generated`,
    same entity_id → overlap. This is what "revising IS using"
    means — listing v1 as used would be a redundant parent edge
    because v2's `derivedFrom` already points at v1."""
    state = stub_state(
        activity_def={"name": "neemBeslissing", "built_in": False},
        used_refs=[_local_used_ref(E_A, V_A1)],
        generated_items=[_local_generated_item(E_A, V_A2)],
    )
    with pytest.raises(ActivityError) as exc:
        enforce_used_generated_disjoint(state)
    assert exc.value.status_code == 422
    assert exc.value.payload["error"] == "used_generated_overlap"
    overlaps = exc.value.payload["overlaps"]
    assert len(overlaps) == 1
    assert overlaps[0]["kind"] == "local"
    assert overlaps[0]["entity_id"] == str(E_A)


def test_local_no_overlap_different_entity_ids(stub_state):
    """Different logical entities — no overlap, phase returns cleanly.
    This is the common case for every non-revision activity."""
    state = stub_state(
        activity_def={"name": "neemBeslissing", "built_in": False},
        used_refs=[_local_used_ref(E_A, V_A1)],
        generated_items=[_local_generated_item(E_B, V_B1)],
    )
    enforce_used_generated_disjoint(state)  # no raise


def test_external_overlap_by_uri(stub_state):
    """External URIs overlap when the exact same URI appears on
    both sides. Externals have no entity_id, just the URI string,
    so the comparison is whole-string."""
    uri = "https://id.erfgoed.net/erfgoedobjecten/60001"
    state = stub_state(
        activity_def={"name": "linkObject", "built_in": False},
        used_refs=[_external_used_ref(uri)],
        generated_items=[{"entity": uri, "content": {}}],
    )
    with pytest.raises(ActivityError) as exc:
        enforce_used_generated_disjoint(state)
    overlaps = exc.value.payload["overlaps"]
    assert len(overlaps) == 1
    assert overlaps[0]["kind"] == "external"
    assert overlaps[0]["entity"] == uri


def test_external_different_uris_no_overlap(stub_state):
    uri_a = "https://id.erfgoed.net/erfgoedobjecten/111"
    uri_b = "https://id.erfgoed.net/erfgoedobjecten/222"
    state = stub_state(
        activity_def={"name": "linkObject", "built_in": False},
        used_refs=[_external_used_ref(uri_a)],
        generated_items=[{"entity": uri_b, "content": {}}],
    )
    enforce_used_generated_disjoint(state)  # no raise


def test_multiple_overlaps_reported_together(stub_state):
    """If several logical entities overlap, the error payload lists
    them all — the user should be able to fix the request in one
    round trip, not one entity at a time."""
    state = stub_state(
        activity_def={"name": "bulkRevise", "built_in": False},
        used_refs=[
            _local_used_ref(E_A, V_A1),
            _local_used_ref(E_B, V_B1),
        ],
        generated_items=[
            _local_generated_item(E_A, V_A2),
            _local_generated_item(E_B, uuid4()),
        ],
    )
    with pytest.raises(ActivityError) as exc:
        enforce_used_generated_disjoint(state)
    overlaps = exc.value.payload["overlaps"]
    assert len(overlaps) == 2
    reported_ids = {o["entity_id"] for o in overlaps}
    assert reported_ids == {str(E_A), str(E_B)}


def test_mixed_local_and_external_overlap(stub_state):
    """A single activity can hit both kinds of overlap in one shot.
    The payload should carry both entries with their respective
    `kind` markers so the client can distinguish them."""
    uri = "https://example.org/foo"
    state = stub_state(
        activity_def={"name": "crossOver", "built_in": False},
        used_refs=[
            _local_used_ref(E_A, V_A1),
            _external_used_ref(uri),
        ],
        generated_items=[
            _local_generated_item(E_A, V_A2),
            {"entity": uri, "content": {}},
        ],
    )
    with pytest.raises(ActivityError) as exc:
        enforce_used_generated_disjoint(state)
    overlaps = exc.value.payload["overlaps"]
    assert len(overlaps) == 2
    kinds = {o["kind"] for o in overlaps}
    assert kinds == {"local", "external"}
