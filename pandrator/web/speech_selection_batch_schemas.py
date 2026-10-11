"""Strict requests for an atomic set of speech selections."""

from pydantic import Field, model_validator

from pandrator.logic.speech_performance import StrictModel

from .speech_selection_schemas import SpeechSelectionRequest


class SpeechSelectionBatchRequest(StrictModel):
    changes: list[SpeechSelectionRequest] = Field(min_length=1, max_length=100)
    detailed_preview: bool = False

    @model_validator(mode="after")
    def one_revision(self):
        if len({item.revision_id for item in self.changes}) != 1:
            raise ValueError("All speech selections must use one plan revision.")
        return self


class SpeechSelectionBatchApplyRequest(SpeechSelectionBatchRequest):
    expected_preview_revision: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$"
    )


SPEECH_SELECTION_BATCH_SCHEMAS = {
    cls.__name__: cls
    for cls in (SpeechSelectionBatchRequest, SpeechSelectionBatchApplyRequest)
}
