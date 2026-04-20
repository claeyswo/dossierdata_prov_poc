"""
Workflow-scoped constants for the toelatingen plugin.

These values are accessible from handlers, hooks, task handlers, and
relation validators via ``context.constants`` / ``plugin.constants``.

Precedence (highest wins):

1. **Environment variables** — ``DOSSIER_TOELATINGEN_...`` prefixed,
   case-insensitive. E.g. ``DOSSIER_TOELATINGEN_AANVRAAG_DEADLINE_DAYS=60``.
   Use for secrets and per-deployment overrides.
2. **workflow.yaml** — ``constants.values`` block. Use for domain-level
   tuning that's the same across environments (and committable).
3. **Class defaults** — what you see below. Sensible defaults so a
   bare workflow.yaml with no constants block still works.

Secrets (API keys, signing keys) should ONLY come from env vars —
never committed to workflow.yaml. The Pydantic types still apply;
an env var that can't parse as the declared type fails at startup.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class ToelatingenConstants(BaseSettings):
    """Typed workflow constants for the toelatingen plugin."""

    model_config = SettingsConfigDict(
        env_prefix="DOSSIER_TOELATINGEN_",
        case_sensitive=False,
        frozen=True,
        extra="ignore",
    )

    # --- Domain-level deadlines (same across environments) ---
    aanvraag_deadline_days: int = 30
    """Days a klaar_voor_behandeling dossier stays actionable before
    the trekAanvraagIn task fires to close it automatically."""

    handtekening_validity_days: int = 90
    """Days a handtekening entity remains valid after creation. Tasks
    and downstream activities treat older signatures as stale."""

    max_bijlagen_per_aanvraag: int = 20
    """Maximum attachment files per aanvraag. Enforced in handlers
    and relation validators."""

    # --- External service URLs (vary by environment) ---
    erfgoed_api_url: str = "https://inventaris.onroerenderfgoed.be"
    """Base URL for resolving erfgoedobject URIs. Dev uses a local
    mock; prod uses the live inventory."""

    # --- Feature flags (vary by environment) ---
    auto_approve_enabled: bool = False
    """When true, aanvragen meeting certain criteria skip the manual
    beslissing step. Typically true only in dev/staging."""

    require_otp_on_signing: bool = True
    """When true, tekenBeslissing requires an OTP challenge. Disable
    in dev so developers can test the sign flow without SMS."""
