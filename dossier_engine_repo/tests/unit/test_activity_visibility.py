"""Unit tests for ``routes._activity_visibility``.

This module's public surface is ``parse_activity_view`` (and its
companion :class:`ActivityViewMode` dataclass plus the
:func:`is_activity_visible` evaluator). Prior to Round 31 there
were no dedicated unit tests for ``parse_activity_view`` — the
function was exercised only via end-to-end route tests, which
meant any contract question ("what happens when I pass X?")
required running the full app to answer.

These tests add the missing unit-level coverage and also pin the
Round 31 removal of the ``"related"`` mode. ``"related"`` was
removed because it wasn't used in production (config.yaml never
wrote it, toelatingen never deployed it) and was adding
uncertainty to every read of the module — a third mode nobody
meant. After Round 31, ``activity_view`` is one of:

* ``"all"`` — every activity visible
* ``"own"`` — only activities where the user is the PROV agent
* ``list[str]`` — only activities of the listed types
* ``dict`` with ``mode: "own"`` + ``include: [...]`` — own plus
  an unconditional-include list

Strings that aren't ``"all"`` or ``"own"`` (including legacy
``"related"`` and anything else) fall through to the deny-safe
default: ``base="list", explicit_types=frozenset()`` — i.e. a
list of zero acceptable types, so nothing is visible. This
matches the existing "unrecognized shape" behaviour and avoids a
silent semantic change for any config still carrying
``"related"`` in dossier_access entities. Operators see an empty
timeline and investigate; the alternative (silently remap to
``"own"``) would hide the config drift.
"""
from __future__ import annotations

from dossier_engine.routes._activity_visibility import (
    ActivityViewMode, parse_activity_view,
)


class TestParseActivityView:

    def test_all_string_returns_base_all(self):
        result = parse_activity_view("all")
        assert result.base == "all"
        assert result.include == frozenset()
        assert result.explicit_types == frozenset()

    def test_none_returns_base_all(self):
        """``None`` means ``activity_view`` wasn't specified in the
        access entry. The module-level convention is that absent
        = unrestricted (``"all"``) — matches ``get_visibility_from_entry``'s
        default behaviour."""
        result = parse_activity_view(None)
        assert result.base == "all"

    def test_own_string_returns_base_own(self):
        result = parse_activity_view("own")
        assert result.base == "own"
        assert result.include == frozenset()

    def test_related_string_falls_through_to_deny_safe(self):
        """Round 31 removed ``"related"`` as a supported mode. Configs
        still carrying it fall through to deny-safe (empty
        explicit_types list) rather than being silently remapped to
        ``"own"`` — see module docstring for rationale. This is a
        deprecation pin: if a future round accidentally restores the
        ``"related"`` branch, this test goes red."""
        result = parse_activity_view("related")
        assert result.base == "list"
        assert result.explicit_types == frozenset()

    def test_unrecognized_string_falls_through_to_deny_safe(self):
        """Same shape as ``"related"`` — unknown string values get
        deny-safe treatment."""
        result = parse_activity_view("banana")
        assert result.base == "list"
        assert result.explicit_types == frozenset()

    def test_list_returns_base_list_with_explicit_types(self):
        result = parse_activity_view(
            ["dienAanvraagIn", "neemBeslissing"],
        )
        assert result.base == "list"
        assert result.explicit_types == frozenset(
            {"dienAanvraagIn", "neemBeslissing"}
        )

    def test_empty_list_returns_deny_safe(self):
        """An explicit empty list is the deny-all shape. Matches
        the ``visible_types = set()`` convention elsewhere in the
        access module."""
        result = parse_activity_view([])
        assert result.base == "list"
        assert result.explicit_types == frozenset()

    def test_dict_with_mode_own_and_include(self):
        """The dict shape combines a base mode with an
        unconditional-include list. Semantically: "show me
        activities under the base rule, PLUS always show any
        activity whose type is in ``include`` regardless of who
        performed it."""
        result = parse_activity_view({
            "mode": "own",
            "include": ["neemBeslissing"],
        })
        assert result.base == "own"
        assert result.include == frozenset({"neemBeslissing"})

    def test_dict_with_list_as_mode_returns_list_with_include(self):
        """Edge case: the dict's ``mode`` can itself be a list, in
        which case it behaves like an explicit_types list combined
        with an include. This is a belt-and-braces form rarely
        used; pin it so the behaviour stays predictable."""
        result = parse_activity_view({
            "mode": ["dienAanvraagIn"],
            "include": ["neemBeslissing"],
        })
        assert result.base == "list"
        assert result.explicit_types == frozenset({"dienAanvraagIn"})
        assert result.include == frozenset({"neemBeslissing"})

    def test_dict_with_unrecognized_mode_still_applies_include(self):
        """If the dict's ``mode`` is something like ``"related"``
        (removed) or an unknown string, the include list still
        applies but the base becomes that unrecognized value — and
        ``is_activity_visible`` will deny-safe it at evaluation
        time because no branch matches the unrecognized base. Pin
        the shape so a future refactor that tries to be 'smarter'
        about unknown modes gets caught."""
        result = parse_activity_view({
            "mode": "related",  # legacy, removed in Round 31
            "include": ["neemBeslissing"],
        })
        # The base value is preserved as-is; is_activity_visible's
        # final ``return False`` covers the evaluation side.
        assert result.base == "related"
        assert result.include == frozenset({"neemBeslissing"})

    def test_unrecognized_type_returns_deny_safe(self):
        """Integer, bool, etc. — anything that isn't str/list/dict/None
        falls through to the catch-all at the end of the function.
        Keeps the function total."""
        # type: ignore[arg-type] intentional — testing bad inputs.
        result = parse_activity_view(42)  # type: ignore[arg-type]
        assert result.base == "list"
        assert result.explicit_types == frozenset()
