"""
Namespace registry — prefix → IRI map for the running app.

The engine supports mixing multiple RDF vocabularies in a single
workflow: `oe:aanvraag` (your own ontology), `foaf:Person` (FOAF),
`dcterms:BibliographicResource` (Dublin Core), `prov:Activity` (PROV
itself). Each prefix used in entity types, relations, or PROV output
must be declared so the engine can:

* Expand a qualified type name to a full IRI when rendering PROV-JSON
* Validate that no workflow YAML uses an undeclared prefix (typo
  protection at plugin load time)
* Emit a complete ``prefixes`` block in PROV-JSON output

The registry is populated once at app startup and consulted everywhere
a qualified name needs expansion or validation.

Sources of declarations (merged in order):

1. **Built-in defaults** — always present:
   ``prov``, ``xsd``, ``rdfs``, ``rdf``, ``dossier`` (dossier-specific,
   template with ``{dossier_id}``)

2. **App-level config** (``config.yaml``'s ``namespaces:`` block):
   ``iri_base.ontology`` defines the default workflow prefix ``oe``.
   Additional application-wide prefixes go in ``namespaces:``.

3. **Plugin-level declarations** (each workflow's ``namespaces:`` block):
   Workflow-specific prefixes. Don't override built-ins; conflicts at
   plugin load raise a clear error.

Thread-safety: the registry is written once at startup and only read
thereafter. No locking needed for the common path.
"""

from __future__ import annotations

# Built-in RDF/PROV namespaces always available. `dossier:` is
# handled specially (per-dossier template) by prov_iris.
#
# `system:` covers engine-provided entity and activity types
# (system:task, system:note, system:exception). These types are
# baked into the engine, not declared by plugin YAML, so plugins
# can use them in their entity_types/used/generated lists without
# also declaring the prefix in their `namespaces:` block. The IRI
# anchors at the engine's own ontology root rather than any plugin's
# domain — this is mechanism, not policy.
_BUILTIN_NAMESPACES: dict[str, str] = {
    "prov": "http://www.w3.org/ns/prov#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "system": "https://dossier-platform.example/vocab/system#",
}


class NamespaceRegistry:
    """Mutable prefix → IRI map, finalised at plugin-load time.

    Usage::

        reg = NamespaceRegistry()
        reg.register("oe", "https://id.erfgoed.net/vocab/ontology#")
        reg.register("foaf", "http://xmlns.com/foaf/0.1/")
        reg.default_workflow_prefix = "oe"

        reg.expand("foaf:Person")
        # → "http://xmlns.com/foaf/0.1/Person"

        reg.validate_type("foaf:Person")  # ok
        reg.validate_type("foo:Bar")       # raises ValueError

    The registry starts pre-populated with the built-in RDF/PROV
    prefixes. Don't register over those — it raises.
    """

    def __init__(self):
        self._map: dict[str, str] = dict(_BUILTIN_NAMESPACES)
        self._builtin_keys = set(_BUILTIN_NAMESPACES.keys())
        # The prefix used for unqualified types like `aanvraag`.
        # Workflows can set this in their YAML; otherwise falls back
        # to "oe" for backwards compatibility.
        self.default_workflow_prefix: str = "oe"

    def register(self, prefix: str, iri: str) -> None:
        """Add a prefix → IRI mapping. Idempotent if same iri.

        Raises ValueError if:
        * the prefix shadows a built-in (``prov``, ``xsd``, etc.)
        * the prefix is already registered to a different IRI
        * the IRI doesn't end in ``#`` or ``/`` (RDF convention)
        """
        if prefix in self._builtin_keys:
            raise ValueError(
                f"Cannot override built-in namespace prefix '{prefix}' "
                f"(fixed to {_BUILTIN_NAMESPACES[prefix]!r})"
            )
        if not (iri.endswith("#") or iri.endswith("/")):
            raise ValueError(
                f"Namespace IRI for prefix '{prefix}' should end with "
                f"'#' or '/'; got {iri!r}"
            )
        existing = self._map.get(prefix)
        if existing and existing != iri:
            raise ValueError(
                f"Prefix '{prefix}' already registered to {existing!r}, "
                f"refusing to rebind to {iri!r}"
            )
        self._map[prefix] = iri

    def __contains__(self, prefix: str) -> bool:
        return prefix in self._map

    def iri_for(self, prefix: str) -> str | None:
        """Return the IRI for a prefix, or None if unknown."""
        return self._map.get(prefix)

    def expand(self, qname: str) -> str:
        """Expand ``prefix:LocalName`` to a full IRI.

        For unqualified names (no colon), uses ``default_workflow_prefix``.
        Returns the input unchanged if the prefix is unknown — callers
        who care about validity should call ``validate_type`` first.
        """
        if ":" not in qname:
            iri = self._map.get(self.default_workflow_prefix)
            return (iri + qname) if iri else qname
        prefix, local = qname.split(":", 1)
        iri = self._map.get(prefix)
        return (iri + local) if iri else qname

    def validate_type(self, qname: str) -> None:
        """Check that a qualified type name uses a declared prefix.

        Raises ValueError with a list of available prefixes on failure.
        Unqualified names (no colon) always pass — they use the default
        prefix.
        """
        if ":" not in qname:
            return
        prefix = qname.split(":", 1)[0]
        if prefix not in self._map:
            available = sorted(self._map.keys())
            raise ValueError(
                f"Unknown namespace prefix '{prefix}' in type {qname!r}. "
                f"Declared prefixes: {available}. Add to your workflow's "
                f"`namespaces:` block or the app config."
            )

    def as_dict(self) -> dict[str, str]:
        """Return a copy of the prefix → IRI map.

        Useful for PROV-JSON prefix blocks or debugging. Does NOT
        include ``dossier:`` since that's per-dossier; callers
        building PROV output add it separately.
        """
        return dict(self._map)


# Singleton instance populated at app startup. Accessed by routes,
# pipeline phases, and PROV export. Tests that want isolation can
# reassign `_instance` or use the `namespaces()` accessor.
_instance: NamespaceRegistry | None = None


def namespaces() -> NamespaceRegistry:
    """Return the global namespace registry.

    Raises if no registry has been configured yet. Every FastAPI app
    created via ``create_app`` configures this at startup; tests that
    bypass that entry point should call ``set_namespaces()`` manually.
    """
    if _instance is None:
        raise RuntimeError(
            "Namespace registry not configured. "
            "Call set_namespaces(registry) first, or use create_app()."
        )
    return _instance


def set_namespaces(registry: NamespaceRegistry) -> None:
    """Install the global namespace registry. Idempotent."""
    global _instance
    _instance = registry


def reset_namespaces() -> None:
    """Clear the global namespace registry. For test isolation."""
    global _instance
    _instance = None
