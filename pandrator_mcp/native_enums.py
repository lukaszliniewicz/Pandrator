"""Runtime annotation bridge for nullable native MCP enum inputs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, TypeAlias, TypeVar

from pydantic import GetCoreSchemaHandler, GetPydanticSchema
from pydantic_core import core_schema


def native_nullable_enum(annotation: Any) -> Any:
    """Preserve the declared nullable enum schema while bypassing SDK JSON parsing."""

    def schema(_source_type: Any, handler: GetCoreSchemaHandler) -> core_schema.CoreSchema:
        return handler.generate_schema(annotation)

    return Annotated[str, GetPydanticSchema(schema)]


if TYPE_CHECKING:
    _EnumValue = TypeVar("_EnumValue", bound=str)
    NativeNullableEnum: TypeAlias = _EnumValue | None
else:

    class NativeNullableEnum:
        """Keep a string SDK base and validate the original Literal union."""

        def __class_getitem__(cls, choices: Any) -> Any:
            return native_nullable_enum(choices | None)
