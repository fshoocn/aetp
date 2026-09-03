""" Artifact、脚本和配置快照 DTO。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from .errors import ErrorCode
from .ids import BusinessId, JsonObject, PluginId, RelativePath, Sha256


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtifactKind(StrEnum):
    REPORT = "report"
    LOG_ARCHIVE = "log_archive"
    DATA = "data"


class ArtifactRef(_Strict):
    artifact_id: BusinessId
    project_id: BusinessId
    run_id: BusinessId | None = None
    shard_id: BusinessId | None = None
    attempt_id: BusinessId | None = None
    node_id: BusinessId | None = None
    kind: ArtifactKind
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=0)
    sha256: Sha256
    derived_from: BusinessId | None = None


class ScriptRef(_Strict):
    script_id: BusinessId
    version: int = Field(ge=1)
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=0)
    sha256: Sha256
    download_url: str | None = None


class ConfigurationSchema(_Strict):
    schema_version: int = Field(ge=1)
    schema_hash: Sha256
    document: JsonObject
    ui_entry: RelativePath | None = None


class Configuration(_Strict):
    schema_version: int = Field(ge=1)
    schema_hash: Sha256
    values: JsonObject


class CaseSelection(_Strict):
    selected_keys: tuple[str, ...] = ()
    include_all: bool = False

    @model_validator(mode="after")
    def validate_selection(self) -> CaseSelection:
        if self.include_all and self.selected_keys:
            raise ValueError("include_all cannot be combined with selected_keys")
        if len(self.selected_keys) != len(set(self.selected_keys)):
            raise ValueError("selected_keys must be unique")
        return self


class TestCase(_Strict):
    stable_key: str = Field(min_length=1, max_length=512)
    name: str = Field(min_length=1, max_length=512)
    parent_path: str = ""
    tags: tuple[str, ...] = ()
    estimated_duration_s: float | None = Field(default=None, ge=0)


class TestPlan(_Strict):
    cases: tuple[TestCase, ...]
    default_case_keys: tuple[str, ...]
    sharding_point: PluginId | None = None

    @model_validator(mode="after")
    def validate_cases(self) -> TestPlan:
        case_keys = tuple(case.stable_key for case in self.cases)
        if len(case_keys) != len(set(case_keys)):
            raise ValueError("case stable_key must be unique")
        if not set(self.default_case_keys).issubset(case_keys):
            raise ValueError("default_case_keys must refer to cases")
        return self


class ValidationIssue(_Strict):
    code: ErrorCode
    path: str = ""
    message: str


class PluginEvent(_Strict):
    event_type: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    project_id: BusinessId | None = None
    run_id: BusinessId | None = None
    payload: JsonObject = Field(default_factory=dict)


class ArtifactWriteRequest(_Strict):
    kind: ArtifactKind
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = "application/octet-stream"


class SecretReference(RootModel[str]):
    root: str = Field(
        pattern=r"^secret://project/[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*(?:/[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)*$"
    )


TestPlan.model_rebuild()
ValidationIssue.model_rebuild()
