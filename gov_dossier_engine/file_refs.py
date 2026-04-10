"""
File reference type and download_url injection.

A `FileId` is a `str` subclass. Fields declared as `FileId` (or `Optional[FileId]`)
on an entity model trigger automatic injection of a signed `download_url`
sibling key in GET responses.

Naming rule for the sibling key:
- if the field name ends in `_id`, replace `_id` with `_download_url`
- otherwise, append `_download_url`

Examples:
    file_id        -> file_download_url
    brief          -> brief_download_url
    signed_pdf_id  -> signed_pdf_download_url

Usage in plugin entity models:

    from gov_dossier_engine.file_refs import FileId

    class Bijlage(BaseModel):
        file_id: FileId
        filename: str

    class Beslissing(BaseModel):
        brief: FileId           # signed decision letter PDF

The route layer calls `inject_download_urls(model_class, content_dict, sign_fn)`
to walk the stored dict and emit a new dict with sibling URLs added.

Why this design (vs an Annotated metadata marker):
- `__get_pydantic_core_schema__` is the documented public extension point
  for custom types in Pydantic v2. No reliance on `field.metadata` or other
  internals that have shifted between v2.x releases.
- `isinstance(value, FileId)` semantics work everywhere: dict validation,
  JSON round-trips, Optional, nested lists.
- The walker uses `Model.model_fields` and `field.annotation` (both public,
  documented v2 API) plus `typing.get_args` / `typing.get_origin` from the
  stdlib. Nothing else.
- The walker copies values from the original dict rather than re-validating.
  This preserves extra/legacy fields and tolerates schema drift on read,
  matching the behavior of the previous (recursive) injector.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, get_args, get_origin

from pydantic import BaseModel, GetCoreSchemaHandler
from pydantic_core import core_schema


# --------------------------------------------------------------------------
# FileId type
# --------------------------------------------------------------------------

class FileId(str):
    """A string subclass that marks a field as holding a file id.

    Validates as a plain string but is wrapped in this subclass on the way
    out so `isinstance(x, FileId)` is True throughout. JSON round-trips and
    `model_validate`/`model_dump` both preserve the type.
    """

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        # Validate input as str, then wrap in FileId via the after-validator.
        return core_schema.no_info_after_validator_function(
            cls,
            core_schema.str_schema(),
        )


# --------------------------------------------------------------------------
# Walker
# --------------------------------------------------------------------------

SignFn = Callable[[str], str]
"""Takes a file id, returns a signed download URL."""


def _default_url_field_name(field_name: str) -> str:
    if field_name.endswith("_id"):
        return field_name[:-3] + "_download_url"
    return field_name + "_download_url"


def _annotation_contains_file_id(annotation: Any) -> bool:
    """True if `annotation` declares a FileId, including inside Optional/Union."""
    if annotation is FileId:
        return True
    for arg in get_args(annotation):
        if arg is FileId:
            return True
    return False


def _basemodel_inside(annotation: Any) -> Optional[type[BaseModel]]:
    """If `annotation` declares a BaseModel subclass — directly, inside
    list[...], or inside Optional[...] — return that subclass. Otherwise None."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    origin = get_origin(annotation)
    if origin in (list, tuple):
        args = get_args(annotation)
        if args and isinstance(args[0], type) and issubclass(args[0], BaseModel):
            return args[0]
    if origin is not None:
        for arg in get_args(annotation):
            if arg is type(None):
                continue
            if isinstance(arg, type) and issubclass(arg, BaseModel):
                return arg
    return None


def _is_list_field(annotation: Any) -> bool:
    return get_origin(annotation) in (list, tuple)


def _walk_dict(
    model_class: type[BaseModel],
    data: dict,
    sign: SignFn,
) -> dict:
    """Walk `data` using `model_class` to learn which fields are FileIds and
    which are nested models. Does NOT validate `data` — values are copied
    through unchanged so extra/legacy keys survive."""
    fields = model_class.model_fields
    out: dict = {}

    for key, value in data.items():
        field = fields.get(key)
        if field is None:
            # Unknown field — pass through unchanged.
            out[key] = value
            continue

        annotation = field.annotation

        if _annotation_contains_file_id(annotation):
            out[key] = value
            if isinstance(value, str):
                sibling = _default_url_field_name(key)
                out[sibling] = sign(value)
            # If the value is None (Optional[FileId]) we leave the field as
            # None and emit no sibling.
            continue

        nested = _basemodel_inside(annotation)
        if nested is not None:
            if _is_list_field(annotation) and isinstance(value, list):
                out[key] = [
                    _walk_dict(nested, item, sign) if isinstance(item, dict) else item
                    for item in value
                ]
            elif isinstance(value, dict):
                out[key] = _walk_dict(nested, value, sign)
            else:
                out[key] = value
            continue

        out[key] = value

    return out


def inject_download_urls(
    model_class: Optional[type[BaseModel]],
    content: Any,
    sign: SignFn,
) -> Any:
    """Walk `content` and inject download_url siblings for any FileId fields.

    If `model_class` is None (no model registered for this entity type) or
    `content` is not a dict, returns the content unchanged.
    """
    if model_class is None or content is None:
        return content
    if not isinstance(content, dict):
        return content
    return _walk_dict(model_class, content, sign)
