"""
Custom validators for toelatingen.

Exception-grant validation has moved to the engine. See
``dossier_engine.builtins.exceptions.valideer_exception`` — it's
auto-wired by the engine for workflows that declare ``exceptions:``
in their top-level YAML.
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
