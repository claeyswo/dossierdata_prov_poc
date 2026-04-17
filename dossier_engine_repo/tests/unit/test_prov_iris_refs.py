"""
Unit tests for domain-relation ref expansion and classification.
"""

from dossier_engine.prov_iris import expand_ref, classify_ref


class TestExpandRef:
    """expand_ref translates shorthand refs to full IRIs."""

    def test_local_entity(self):
        """oe:type/eid@vid → full entity IRI in the given dossier."""
        result = expand_ref(
            "oe:aanvraag/e1000000-0000-0000-0000-000000000001@f1000000-0000-0000-0000-000000000001",
            "d1000000-0000-0000-0000-000000000001",
        )
        assert result == (
            "https://id.erfgoed.net/dossiers/"
            "d1000000-0000-0000-0000-000000000001/entities/"
            "oe:aanvraag/e1000000-0000-0000-0000-000000000001/"
            "f1000000-0000-0000-0000-000000000001"
        )

    def test_cross_dossier_entity(self):
        """dossier:did/oe:type/eid@vid → entity IRI in other dossier."""
        result = expand_ref(
            "dossier:d2000000-0000-0000-0000-000000000001/"
            "oe:aanvraag/e1000000-0000-0000-0000-000000000001@f1000000-0000-0000-0000-000000000001",
            "d1000000-0000-0000-0000-000000000001",
        )
        assert result == (
            "https://id.erfgoed.net/dossiers/"
            "d2000000-0000-0000-0000-000000000001/entities/"
            "oe:aanvraag/e1000000-0000-0000-0000-000000000001/"
            "f1000000-0000-0000-0000-000000000001"
        )

    def test_dossier_ref(self):
        """dossier:did → dossier base IRI."""
        result = expand_ref(
            "dossier:d2000000-0000-0000-0000-000000000001",
            "d1000000-0000-0000-0000-000000000001",
        )
        assert result == (
            "https://id.erfgoed.net/dossiers/"
            "d2000000-0000-0000-0000-000000000001/"
        )

    def test_external_uri_passthrough(self):
        """https:// URIs are returned unchanged."""
        uri = "https://id.erfgoed.net/erfgoedobjecten/10001"
        assert expand_ref(uri, "d1") == uri

    def test_http_uri_passthrough(self):
        """http:// URIs are also returned unchanged."""
        uri = "http://example.com/thing"
        assert expand_ref(uri, "d1") == uri

    def test_unparseable_ref_returned_as_is(self):
        """If the ref doesn't match any known format, return it as-is
        and let downstream validation catch it."""
        assert expand_ref("garbage", "d1") == "garbage"


class TestClassifyRef:
    """classify_ref determines the kind of a domain-relation ref."""

    def test_local_entity_shorthand(self):
        assert classify_ref("oe:aanvraag/e1000000-0000-0000-0000-000000000001@f1000000-0000-0000-0000-000000000001") == "entity"

    def test_cross_dossier_entity_shorthand(self):
        assert classify_ref("dossier:d2/oe:aanvraag/e1000000-0000-0000-0000-000000000001@f1000000-0000-0000-0000-000000000001") == "entity"

    def test_dossier_shorthand(self):
        assert classify_ref("dossier:d2000000-0000-0000-0000-000000000001") == "dossier"

    def test_external_uri(self):
        assert classify_ref("https://id.erfgoed.net/erfgoedobjecten/10001") == "external_uri"

    def test_expanded_entity_iri(self):
        """Already-expanded entity IRI is classified as entity."""
        iri = "https://id.erfgoed.net/dossiers/d1/entities/oe:aanvraag/e1/v1"
        assert classify_ref(iri) == "entity"

    def test_expanded_dossier_iri(self):
        """Already-expanded dossier IRI is classified as dossier."""
        iri = "https://id.erfgoed.net/dossiers/d2/"
        assert classify_ref(iri) == "dossier"

    def test_non_platform_https(self):
        """An HTTPS URI not under the dossier base is external."""
        assert classify_ref("https://codex.vlaanderen.be/artikel/17") == "external_uri"


class TestValidateRefTypes:
    """_validate_ref_types checks from/to against declared constraints."""

    def test_valid_entity_to_external(self):
        """entity → external_uri passes when declared."""
        from dossier_engine.engine.pipeline.relations import _validate_ref_types
        # Should not raise
        _validate_ref_types(
            "oe:betreft",
            "oe:aanvraag/e1000000-0000-0000-0000-000000000001@f1000000-0000-0000-0000-000000000001",
            "https://id.erfgoed.net/erfgoedobjecten/10001",
            {"from_types": ["entity"], "to_types": ["external_uri"]},
        )

    def test_wrong_from_type_rejected(self):
        """dossier ref as 'from' when only entity is allowed → 422."""
        from dossier_engine.engine.pipeline.relations import _validate_ref_types
        from dossier_engine.engine.errors import ActivityError
        import pytest
        with pytest.raises(ActivityError) as exc_info:
            _validate_ref_types(
                "oe:betreft",
                "dossier:d2000000-0000-0000-0000-000000000001",
                "https://id.erfgoed.net/erfgoedobjecten/10001",
                {"from_types": ["entity"], "to_types": ["external_uri"]},
            )
        assert exc_info.value.status_code == 422
        assert "dossier" in str(exc_info.value)

    def test_wrong_to_type_rejected(self):
        """entity ref as 'to' when only external_uri is allowed → 422."""
        from dossier_engine.engine.pipeline.relations import _validate_ref_types
        from dossier_engine.engine.errors import ActivityError
        import pytest
        with pytest.raises(ActivityError) as exc_info:
            _validate_ref_types(
                "oe:betreft",
                "oe:aanvraag/e1000000-0000-0000-0000-000000000001@f1000000-0000-0000-0000-000000000001",
                "oe:beslissing/e2000000-0000-0000-0000-000000000001@f2000000-0000-0000-0000-000000000001",
                {"from_types": ["entity"], "to_types": ["external_uri"]},
            )
        assert exc_info.value.status_code == 422
        assert "entity" in str(exc_info.value)

    def test_dossier_to_dossier_valid(self):
        """dossier → dossier passes for gerelateerd_aan."""
        from dossier_engine.engine.pipeline.relations import _validate_ref_types
        _validate_ref_types(
            "oe:gerelateerd_aan",
            "dossier:d1",
            "dossier:d2",
            {"from_types": ["dossier"], "to_types": ["dossier"]},
        )

    def test_no_constraints_skips_validation(self):
        """When from_types/to_types are absent, anything passes."""
        from dossier_engine.engine.pipeline.relations import _validate_ref_types
        _validate_ref_types(
            "oe:custom",
            "dossier:d1",
            "https://example.com/whatever",
            {},  # no from_types, no to_types
        )

    def test_multiple_allowed_types(self):
        """from_types can allow multiple kinds."""
        from dossier_engine.engine.pipeline.relations import _validate_ref_types
        _validate_ref_types(
            "oe:mixed",
            "dossier:d1",
            "https://example.com/thing",
            {"from_types": ["entity", "dossier"], "to_types": ["external_uri"]},
        )
