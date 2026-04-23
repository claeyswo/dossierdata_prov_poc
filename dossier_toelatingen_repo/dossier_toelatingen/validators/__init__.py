"""
Custom validators for toelatingen.
"""

from __future__ import annotations

from dossier_engine.engine import ActivityContext


async def valideer_indiening(context: ActivityContext) -> bool:
    """
    Validates that the aanvraag is complete and valid.
    For POC: always returns True.
    In production: check required fields, external references, etc.
    """
    return True


# Obs 95 / Round 28: the ``VALIDATORS = {"valideer_indiening": ...}``
# registry dict has been removed. Workflow YAML now references this
# callable as ``dossier_toelatingen.validators.valideer_indiening``
# and the engine resolves it at plugin load via
# ``build_callable_registries_from_workflow``.
