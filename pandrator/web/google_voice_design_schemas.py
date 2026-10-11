"""Strict request contract for native Google prompted voice creation."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_LANGUAGE_TAG = r"^[a-zA-Z]{2,3}(?:-[a-zA-Z0-9]{2,8})*$"


class GoogleVoiceDesignCreate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
    )

    service_id: Literal["gemini", "vertex_ai"]
    model: Literal["gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts"]
    name: str = Field(min_length=1, max_length=255)
    prompt: str = Field(min_length=1, max_length=4000)
    language: str = Field(default="en", max_length=40, pattern=_LANGUAGE_TAG)
    voice_category: Literal[
        "male", "female", "androgynous", "unspecified"
    ] = "unspecified"
