"""
Handler-facing types.

Everything a handler or validator function touches at call time lives here:

* `ActivityContext` — passed to handlers and validators. Wraps the repo,
  the dossier id, the resolved `used` entities, and the plugin reference
  so handlers can do typed entity access without knowing about the
  engine's internals.

* `_PendingEntity` — duck-typed stand-in for an `EntityRow` that hasn't
  been persisted yet. Used inside the engine's main loop so handlers can
  read entities the current activity is in the process of generating
  (via `context.get_typed`) before they hit the database.

* `HandlerResult` — what a handler returns: optional content, optional
  status transition, optional generated entities, optional task definitions.

* `TaskResult` — what a cross-dossier task function returns. Tells the
  worker which dossier the resulting activity should land in.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from ..auth import User
from ..db.models import Repository, EntityRow
from ..plugin import Plugin
from .errors import CardinalityError


class _PendingEntity:
    """Lightweight stand-in for an entity that hasn't been persisted yet.

    The engine constructs one of these for every entity the current
    activity is generating, so handlers running in the same activity can
    read them via `context.get_typed()` before the database row exists.
    Quacks like `EntityRow` for **every** column handlers or the
    engine's own walkers might read — not just ``content``.

    Bug 20 (Round 30): ``type``, ``dossier_id``, ``generated_by``,
    ``derived_from``, and ``tombstoned_by`` were missing from earlier
    versions of this class. The concrete production crash path was
    ``_build_trekAanvraag_task`` calling ``find_related_entity`` with
    a pending beslissing; the walker reads ``start_entity.type`` and
    ``start_entity.generated_by`` at the top of its loop and hit
    ``AttributeError``. Structurally reachable when the activity's
    ``used:`` block doesn't include the aanvraag — workflow rules
    normally prevent it, but the class's type surface doesn't.

    When you add a column to ``EntityRow``, also add it here, or
    ``context.get_typed`` (and any walker that reads the row through
    it) will fail with ``AttributeError`` on pending entities.
    ``tests/unit/test_refs_and_plugin.py::TestPendingEntityFieldParity``
    enumerates every ``EntityRow`` column at test time and fails
    loudly if this class drifts again.

    Two fields are invariantly ``None`` for pending entities:

    * ``tombstoned_by`` — pending entities cannot be tombstoned;
      tombstoning happens in the persistence phase, which runs after
      the current activity's pending entities are written out.
    * ``created_at`` — set by the database at INSERT time via the
      column default. Callers asking for the creation time of a
      pending entity are asking the wrong question; use the
      activity's ``started_at`` instead.
    """

    def __init__(
        self,
        content,
        entity_id,
        id,
        attributed_to,
        schema_version=None,
        *,
        type: str | None = None,
        dossier_id=None,
        generated_by=None,
        derived_from=None,
    ):
        self.content = content
        self.entity_id = entity_id
        self.id = id
        self.attributed_to = attributed_to
        self.schema_version = schema_version
        # Bug 20: additional EntityRow columns that handlers + the
        # lineage walker may read. Declared explicitly on the
        # constructor so the caller cannot silently forget one —
        # if any EntityRow column is missing from this signature,
        # the parity test goes red.
        self.type = type
        self.dossier_id = dossier_id
        self.generated_by = generated_by
        self.derived_from = derived_from
        # Invariantly None for pending entities — see class docstring.
        self.tombstoned_by = None
        self.created_at = None


class ActivityContext:
    """Context object passed to plugin handlers and validators.

    Provides typed access to the entities the current activity has used,
    plus a few helpers that hide the cardinality-vs-singleton distinction
    so handlers don't have to think about it.

    For recorded task handlers, `triggering_activity_id` is set to the
    activity that scheduled the task (the task entity's `generated_by`).
    Task handlers that need to operate on the exact entity versions that
    existed at the time the task was scheduled — rather than on the
    current latest versions — can use this to walk back via
    `repo.get_entities_generated_by_activity(...)`.

    User attribution — two fields, always both populated in production::

    * ``user`` — the agent the current code is *executing as*. For a
      direct handler/validator/split-hook this is the request-making
      user; for side effects and worker-run tasks this is the system
      user (the engine / the worker is the executor). Use when asking
      "who is doing this thing right now?".

    * ``triggering_user`` — the agent attributed with the activity that
      *caused* this context to be constructed. For a direct handler,
      this is the same as ``user`` (the request-maker triggered
      themselves). For a side effect, this is the original user whose
      activity started the pipeline. For a worker-run task, this is
      the agent resolved from the triggering activity's association
      row. Use when attributing audit events, denial reasons, or any
      record that says "this happened because of so-and-so's action."

    The split matters most in worker tasks and side effects. Example:
    a user submits an aanvraag with a cross-dossier ``file_id``; the
    ``move_bijlagen_to_permanent`` task runs in the worker, gets a 403
    from the file service, and emits a ``dossier.denied`` audit event.
    The audit event's actor should be the aanvrager (``triggering_user``),
    not the system worker (``user``) — otherwise the SIEM can't
    attribute the rejected graft back to the person who caused it.
    """

    def __init__(
        self,
        repo: Repository,
        dossier_id: UUID,
        used_entities: dict[str, EntityRow],
        entity_models: dict[str, Any] | None = None,
        plugin: Plugin | None = None,
        triggering_activity_id: UUID | None = None,
        *,
        user: User | None = None,
        triggering_user: User | None = None,
    ):
        self.repo = repo
        self.dossier_id = dossier_id
        self._used_entities = used_entities
        self._entity_models = entity_models or {}
        self._plugin = plugin
        self.triggering_activity_id = triggering_activity_id
        # Executor identity. Default None is for test fixtures and
        # adapter code that predates the two-field split; production
        # pipeline and worker construction sites always pass both.
        self.user = user
        # Attribution identity. For direct user requests both fields
        # hold the same User; for side effects and worker tasks they
        # diverge — see the class docstring.
        self.triggering_user = triggering_user

    def get_used_entity(self, entity_type: str) -> EntityRow | None:
        return self._used_entities.get(entity_type)

    def get_used_row(self, entity_type: str) -> EntityRow | None:
        """Return the EntityRow for a used entity of this type. Useful for
        handlers that need the version id to seed a lineage walk."""
        return self._used_entities.get(entity_type)

    @property
    def constants(self) -> Any:
        """The plugin's workflow-scoped constants object.

        Typed Pydantic BaseSettings instance populated at plugin load
        from env vars, workflow.yaml, and class defaults. Use for
        anything that's configuration rather than per-activity data:
        deadline durations, feature flags, external service URLs,
        secrets.

        Returns None if the plugin didn't declare a constants class.
        Accessing attributes on None will raise AttributeError — the
        clearer failure mode is to declare an empty class than to
        leave constants undeclared and silently miss values.
        """
        return self._plugin.constants if self._plugin else None

    def get_typed(self, entity_type: str) -> Any | None:
        """Get a used entity's content as a validated Pydantic model instance.

        Returns None if the entity doesn't exist or has no content (e.g.
        a tombstoned row).

        Routes via `plugin.resolve_schema` so the returned model matches
        the row's stored `schema_version` — this is the read-side of the
        store-version-wins rule. Legacy unversioned rows (schema_version
        is NULL) fall back to `entity_models`.
        """
        entity = self._used_entities.get(entity_type)
        if not entity or not entity.content:
            return None
        if self._plugin is not None:
            model_class = self._plugin.resolve_schema(entity_type, entity.schema_version)
        else:
            model_class = self._entity_models.get(entity_type)
        if model_class:
            return model_class(**entity.content)
        return None

    def _require_singleton(self, entity_type: str) -> None:
        if self._plugin and not self._plugin.is_singleton(entity_type):
            raise CardinalityError(
                f"ActivityContext singleton lookup called on non-singleton "
                f"type '{entity_type}'. Use get_entities_latest(entity_type) "
                f"to iterate instead."
            )

    async def get_singleton_typed(self, entity_type: str) -> Any | None:
        """Get the singleton entity's content as a validated Pydantic model
        instance. Raises `CardinalityError` if called on a non-singleton type."""
        self._require_singleton(entity_type)
        entity = await self.repo.get_singleton_entity(self.dossier_id, entity_type)
        if not entity or not entity.content:
            return None
        if self._plugin is not None:
            model_class = self._plugin.resolve_schema(entity_type, entity.schema_version)
        else:
            model_class = self._entity_models.get(entity_type)
        if model_class:
            return model_class(**entity.content)
        return None

    async def has_activity(self, activity_type: str) -> bool:
        activities = await self.repo.get_activities_for_dossier(self.dossier_id)
        return any(a.type == activity_type for a in activities)

    async def get_singleton_entity(self, entity_type: str) -> EntityRow | None:
        """Return the singleton entity row for this type in the dossier.
        Raises `CardinalityError` if called on a non-singleton type."""
        self._require_singleton(entity_type)
        return await self.repo.get_singleton_entity(self.dossier_id, entity_type)

    async def get_entities_latest(self, entity_type: str) -> list[EntityRow]:
        """Return the latest version of each logical entity of this type.

        Works for both singleton and multi-cardinality types — for
        singletons the list has zero or one elements. For multi-cardinality
        types, one element per distinct entity_id.
        """
        return await self.repo.get_entities_by_type_latest(self.dossier_id, entity_type)


class HandlerResult:
    """Return value from a plugin handler function.

    Handlers can produce any combination of:

    * `content` — convenience for the common case of one generated entity
      whose type is implied by the activity's `generates` block.
    * `generated` — explicit list of entities to create. Each item is
      either a `(type, content)` tuple (legacy) or a dict with explicit
      `type`, `content`, optional `entity_id`, optional `derived_from`.
      Multi-cardinality types must use the dict form to specify which
      logical entity is being revised.
    * `status` — new dossier status, overrides the activity's YAML status.
    * `tasks` — task definitions to schedule. Same shape as the activity's
      YAML `tasks` block.
    """

    def __init__(
        self,
        content: dict | None = None,
        status: str | None = None,
        generated: list | None = None,
        tasks: list[dict] | None = None,
    ):
        # Convenience: single content with no explicit generated list →
        # one generated item with type=None. The engine resolves the type
        # from the activity's `generates[0]` later.
        if content and not generated:
            self.generated = [{"type": None, "content": content}]
        else:
            normalized = []
            for item in (generated or []):
                if isinstance(item, dict):
                    normalized.append(item)
                elif isinstance(item, (tuple, list)) and len(item) == 2:
                    normalized.append({"type": item[0], "content": item[1]})
                else:
                    raise ValueError(f"Invalid HandlerResult.generated item: {item}")
            self.generated = normalized
        self.status = status
        self.tasks = tasks or []


class TaskResult:
    """Return value from a cross-dossier task function.

    The worker uses `target_dossier_id` to know which dossier to land
    the resulting activity in.
    """

    def __init__(self, target_dossier_id: str, content: dict | None = None):
        self.target_dossier_id = target_dossier_id
        self.content = content
