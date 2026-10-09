"""Wire contracts shared by the API and Mac worker."""
from typing import Literal
from uuid import UUID
import re
from pydantic import BaseModel, ConfigDict, Field, model_validator

Operation = Literal["device-info", "package-status", "reboot"]
Status = Literal["USB_READY", "WIFI_FALLBACK", "USB_OFFLINE", "UNKNOWN", "UNAUTHORIZED"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Submit(StrictModel):
    serial: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    operation: Operation
    package: str | None = Field(default=None, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=128)
    allow_wifi: bool = False

    @model_validator(mode="after")
    def validate_parameters(self):
        if self.operation == "package-status":
            if not self.package or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+", self.package):
                raise ValueError("package-status requires a valid Android package name")
        elif self.package is not None:
            raise ValueError("package is only valid for package-status")
        return self


class Observation(StrictModel):
    serial: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    status: Status
    transport_id: int | None = Field(default=None, gt=0)


class Scan(StrictModel):
    instance: UUID
    sequence: int = Field(ge=0)
    complete: bool
    devices: list[Observation] = Field(max_length=500)

    @model_validator(mode="after")
    def unique(self):
        if len({d.serial for d in self.devices}) != len(self.devices):
            raise ValueError("duplicate device serial")
        return self


class Boot(StrictModel):
    instance: UUID


class Claim(Boot):
    pass


class Lease(Boot):
    token: UUID


class Complete(Lease):
    outcome: Literal["succeeded", "failed", "outcome_unknown", "cancelled"]
    # No raw command output, credentials, screenshots or conversation text.
    result: dict[str, str | bool | int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def bounded(self):
        import json
        if len(json.dumps(self.result).encode()) > 4096:
            raise ValueError("result exceeds 4 KiB")
        return self


class Enroll(StrictModel):
    serial: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    project: str = Field(pattern=r"^[a-z0-9_-]{1,64}$")
    worker: str = Field(pattern=r"^[a-z0-9_-]{1,64}$")
    name: str = Field(min_length=1, max_length=100)
    legacy_access_disabled: Literal[True]


class Reconcile(StrictModel):
    outcome: Literal["succeeded", "failed", "cancelled"]
    prior_executor_stopped: Literal[True]
    evidence: str = Field(min_length=10, max_length=1000)
