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


VALIDATORS = {
    "valideer_indiening": valideer_indiening,
}
