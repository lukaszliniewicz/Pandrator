"""Runtime annotation bridges for opaque nullable MCP input strings."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, TypeAlias

from pydantic import Field, GetCoreSchemaHandler, GetPydanticSchema
from pydantic_core import core_schema

_MAX_LEGACY_TEXT_CHARACTERS = 524_288


def _nullable_string_schema(
    source_type: Any, handler: GetCoreSchemaHandler
) -> core_schema.CoreSchema:
    return core_schema.nullable_schema(handler(source_type))


if TYPE_CHECKING:
    NativeNullableString: TypeAlias = str | None
else:
    # The SDK pre-parses JSON for union fields. A string base preserves input;
    # the public nullable schema retains None and each parameter's constraints.
    NativeNullableString = Annotated[str, GetPydanticSchema(_nullable_string_schema)]

NativeResponseText: TypeAlias = Annotated[
    NativeNullableString, Field(max_length=_MAX_LEGACY_TEXT_CHARACTERS)
]
