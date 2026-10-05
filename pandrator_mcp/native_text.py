"""Runtime annotation bridges for opaque nullable MCP input text."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, TypeAlias, cast

from pydantic import Field, ValidatorFunctionWrapHandler, WrapValidator

_MAX_LEGACY_TEXT_CHARACTERS = 524_288


def _nullable_response_text(value: Any, handler: ValidatorFunctionWrapHandler) -> str | None:
    if value is None:
        return None
    return cast(str, handler(value))


if TYPE_CHECKING:
    NativeResponseText: TypeAlias = str | None
else:
    # The SDK pre-parses JSON for union fields. A string base preserves opaque
    # text; the public validator and schema retain the nullable input contract.
    NativeResponseText = Annotated[
        str,
        Field(max_length=_MAX_LEGACY_TEXT_CHARACTERS),
        WrapValidator(
            _nullable_response_text,
            json_schema_input_type=Annotated[
                str | None, Field(max_length=_MAX_LEGACY_TEXT_CHARACTERS)
            ],
        ),
    ]
