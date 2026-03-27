"""
Toelatingen beschermd erfgoed plugin.

Provides:
- workflow definition
- entity models
- handlers
- validators
- task handlers
"""

from __future__ import annotations

import os
import yaml

from gov_dossier_engine.plugin import Plugin
from gov_dossier_engine.entities import DossierAccess

from .entities import (
    Aanvraag,
    Beslissing,
    Handtekening,
    VerantwoordelijkeOrganisatie,
    Behandelaar,
    SystemFields,
)
from .handlers import HANDLERS
from .validators import VALIDATORS
from .tasks import TASK_HANDLERS


def create_plugin() -> Plugin:
    """Create and return the toelatingen plugin."""

    # Load workflow YAML
    workflow_path = os.path.join(os.path.dirname(__file__), "workflow.yaml")
    with open(workflow_path) as f:
        workflow = yaml.safe_load(f)

    # Map entity types to Pydantic models (keyed by type, which is the prefix)
    entity_models = {
        "oe:aanvraag": Aanvraag,
        "oe:beslissing": Beslissing,
        "oe:handtekening": Handtekening,
        "oe:verantwoordelijke_organisatie": VerantwoordelijkeOrganisatie,
        "oe:behandelaar": Behandelaar,
        "oe:system_fields": SystemFields,
        "oe:dossier_access": DossierAccess,
    }

    return Plugin(
        name=workflow["name"],
        workflow=workflow,
        entity_models=entity_models,
        handlers=HANDLERS,
        validators=VALIDATORS,
        task_handlers=TASK_HANDLERS,
    )
