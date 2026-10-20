from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Stamp(StrictModel):
    description: str
    visible_text: str | None = None
    location: str | None = None


class Signature(StrictModel):
    description: str
    associated_name: str | None = None
    location: str | None = None


class OtherMark(StrictModel):
    type: str
    description: str
    location: str | None = None


class VisualElements(StrictModel):
    stamps: list[Stamp] = Field(default_factory=list)
    signatures: list[Signature] = Field(default_factory=list)
    other_marks: list[OtherMark] = Field(default_factory=list)


class PageExtraction(StrictModel):
    transcription: str
    analysis_notes: str
    visual_elements: VisualElements
    context_summary: str


class Analysis(StrictModel):
    document_type: str
    summary: str
    key_points: list[str]


class Transcription(StrictModel):
    text: str


class RequestedOutput(StrictModel):
    user_request: str
    response: str | None = None


class GeneratedRequestedOutput(StrictModel):
    """Only the model-generated part; the backend owns the original user request."""

    response: str | None = None


class DocumentAnalysis(StrictModel):
    analysis: Analysis
    transcription: Transcription
    visual_elements: VisualElements
    requested_output: RequestedOutput


class DocumentAnalysisGeneration(StrictModel):
    """Fields Ollama must synthesize; transcription is assembled from page OCR."""

    analysis: Analysis
    visual_elements: VisualElements
    requested_output: GeneratedRequestedOutput
