"""
File reference annotation and download_url injection.

A `FileId` is a `str` at runtime, but carries metadata that tells the GET
response layer to inject a signed `download_url` sibling next to it.

Usage in plugin entity models:

    from gov_dossier_engine.file_refs import FileId, file_id

    class Bijlage(BaseModel):
        # Default: sibling key will be "<field_name>_download_url".
        file_id: FileId = file_id(url_field="download_url")  # backwards-compat
        filename: str

    class Beslissing(BaseModel):
        brief: FileId  # sibling will be "brief_download_url"

The route layer hydrates raw entity content through the registered Pydantic
model and walks the resulting tree, finding fields annotated with FileId and
calling a sign function to produce the download URL.

Why a class-level marker rather than a recursive scan for the literal key
`"file_id"`: discoverability. Open the entity model file and the file
references are right there in the type annotations, with no field-name
collision risk and no limit to one file per model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Callable, Optional, Union, get_args, get_origin

from pydantic import BaseModel


@dataclass(frozen=True)
class _FileIdMarker:
    """Marker carried inside a `FileId` annotation.

    `url_field`, when set, overrides the default sibling key name. Default
    behavior: if the field name ends in ``_id`` it is replaced with
    ``_download_url``; otherwise ``_download_url`` is appended.
    """
    url_field: Optional[str] = None


def file_id(*, url_field: Optional[str] = None) -> Any:
    """Pydantic field default helper. Use only if you need to override the
    sibling key name; in most cases just annotate as ``FileId``."""
    # Returns the marker itself; the field declaration uses Annotated for
    # type-side metadata, and this helper exists purely for symmetry / docs.
    return _FileIdMarker(url_field=url_field)


# The public type. A FileId is a str carrying a _FileIdMarker in its
# annotation metadata.
FileId = Annotated[str, _FileIdMarker()]


# --------------------------------------------------------------------------
# Walker
# --------------------------------------------------------------------------

SignFn = Callable[[str], str]
"""Takes a file id, returns a signed download URL."""


def _default_url_field_name(field_name: str) -> str:
    if field_name.endswith("_id"):
        return field_name[:-3] + "_download_url"
    return field_name + "_download_url"


def _extract_file_marker_from_metadata(metadata: list) -> Optional[_FileIdMarker]:
    """Pydantic FieldInfo exposes Annotated metadata via .metadata for the
    top-level case (`field: FileId`). For wrapped cases like
    `field: Optional[FileId]`, the metadata stays inside the annotation
    object and we have to dig it out."""
    for meta in metadata or []:
        if isinstance(meta, _FileIdMarker):
            return meta
    return None


def _extract_file_marker_from_annotation(annotation: Any) -> Optional[_FileIdMarker]:
    """Recursively look inside an annotation for a _FileIdMarker. Handles
    Annotated[...], Optional[...], Union[...], and combinations thereof."""
    # Annotated[X, ...] case
    args = get_args(annotation)
    origin = get_origin(annotation)

    # typing.Annotated exposes its metadata via __metadata__ as well
    metadata = getattr(annotation, "__metadata__", None)
    if metadata:
        for meta in metadata:
            if isinstance(meta, _FileIdMarker):
                return meta

    # Recurse into Union/Optional
    if origin is Union:
        for arg in args:
            if arg is type(None):
                continue
            found = _extract_file_marker_from_annotation(arg)
            if found is not None:
                return found
    return None


def _unwrap_optional(annotation: Any) -> Any:
    """Unwrap Optional[X] / Union[X, None] to X. Leaves other unions alone."""
    origin = get_origin(annotation)
    if origin is Union:
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _is_basemodel(tp: Any) -> bool:
    return isinstance(tp, type) and issubclass(tp, BaseModel)


def _walk(value: Any, annotation: Any, sign: SignFn) -> Any:
    """Walk a value alongside its declared type, returning a JSON-ready dict
    or list with download_url siblings injected."""
    annotation = _unwrap_optional(annotation)
    origin = get_origin(annotation)

    # list[X]
    if origin in (list, tuple):
        item_type = get_args(annotation)[0] if get_args(annotation) else Any
        if isinstance(value, list):
            return [_walk(item, item_type, sign) for item in value]
        return value

    # Nested BaseModel
    if _is_basemodel(annotation):
        if isinstance(value, dict):
            return _walk_model(annotation, value, sign)
        return value

    # Plain value (including FileId, which is just str at runtime). FileId
    # is detected at the field level in _walk_model, not here.
    return value


def _walk_model(model_class: type[BaseModel], data: dict, sign: SignFn) -> dict:
    """Walk a dict expected to match `model_class`. Inject download_url
    siblings for FileId-annotated fields. Returns a new dict; does not mutate."""
    if not isinstance(data, dict):
        return data

    out: dict = {}
    fields = model_class.model_fields

    for key, value in data.items():
        field = fields.get(key)
        if field is None:
            # Field not declared on the model — pass through unchanged.
            out[key] = value
            continue

        annotation = field.annotation
        marker = (
            _extract_file_marker_from_metadata(field.metadata)
            or _extract_file_marker_from_annotation(annotation)
        )

        if marker is not None and isinstance(value, str):
            out[key] = value
            sibling = marker.url_field or _default_url_field_name(key)
            out[sibling] = sign(value)
        else:
            out[key] = _walk(value, annotation, sign)

    return out


def inject_download_urls(
    model_class: Optional[type[BaseModel]],
    content: Any,
    sign: SignFn,
) -> Any:
    """Hydrate-walk `content` through `model_class` and inject download URLs.

    If `model_class` is None (no registered model for this entity type),
    returns content unchanged.
    """
    if model_class is None or content is None:
        return content
    if not isinstance(content, dict):
        return content
    return _walk_model(model_class, content, sign)
