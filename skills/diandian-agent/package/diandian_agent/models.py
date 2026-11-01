from datetime import date, time as CalendarTime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Metadata(DTO):
    plannedDate: date | None = None
    time: CalendarTime | None = None
    kind: Literal["task", "event"] = "task"
    event: int | None = Field(default=None, gt=0)
    state: Literal["todo", "doing", "review"] = "todo"


class TaskDraft(DTO):
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=100000)
    priority: int = Field(default=0, ge=0, le=5)
    intent: Literal["self", "agent"] = "self"
    assigneeIds: list[int] = Field(default_factory=list, max_length=100)
    labelIds: list[int] = Field(default_factory=list, max_length=100)
    meta: Metadata = Field(default_factory=Metadata)


class Action(DTO):
    actionId: str = Field(min_length=8, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    operation: Literal["task.create", "task.update", "task.delete", "event.create", "event.update",
                       "project.create", "project.update", "project.archive", "assignee.add",
                       "assignee.remove", "label.add", "label.remove"]
    projectId: int | None = Field(default=None, gt=0)
    taskId: int | None = Field(default=None, gt=0)
    draft: TaskDraft | None = None
    children: list[TaskDraft] = Field(default_factory=list, max_length=100)
    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=100000)
    priority: int | None = Field(default=None, ge=0, le=5)
    meta: Metadata | None = None
    userId: int | None = Field(default=None, gt=0)
    labelId: int | None = Field(default=None, gt=0)
    labelIds: list[int] | None = Field(default=None, max_length=100)
    ifVersion: str | None = Field(default=None, min_length=1, max_length=128)
    dryRun: bool = False
    done: bool | None = None
    archived: bool = True

    @model_validator(mode="after")
    def required_fields(self):
        if self.operation in ("task.create", "event.create"):
            if not self.projectId or not self.draft:
                raise ValueError("projectId and draft required")
        elif self.operation == "project.create":
            if not self.title:
                raise ValueError("title required")
        elif self.operation in ("project.archive", "project.update"):
            if not self.projectId:
                raise ValueError("projectId required")
        elif not self.taskId:
            raise ValueError("taskId required")
        if self.operation.startswith("assignee.") and not self.userId:
            raise ValueError("userId required")
        if self.operation.startswith("label.") and not self.labelId:
            raise ValueError("labelId required")
        if self.operation != "event.create" and self.children:
            raise ValueError("children only valid for event.create")
        if self.labelIds is not None:
            if self.operation not in ("task.update", "event.update") or any(x <= 0 for x in self.labelIds):
                raise ValueError("positive labelIds only valid for updates")
        return self


class Prepare(DTO):
    actionId: str = Field(min_length=8, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    mode: Literal["daily", "project"] = "daily"
    projectId: int | None = Field(default=None, gt=0)
    botId: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def project_required(self):
        if self.mode == "project" and self.projectId is None:
            raise ValueError("projectId required in project mode")
        return self


class PairCode(DTO):
    spaceId: str
    projectIds: list[int] = Field(min_length=1, max_length=100)
    scopes: list[Literal["read", "execute", "manage"]] = Field(default_factory=lambda: ["read", "execute"])


class Redeem(DTO):
    code: str = Field(min_length=1, max_length=128)
    name: str = Field(default="Agent", min_length=1, max_length=100)


class DeviceStart(DTO):
    name: str = Field(default="Agent", min_length=1, max_length=100)

class ClientIdentity(DTO):
    name: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def visible_name(self):
        from .client_identity import clean_name
        self.name = clean_name(self.name)
        return self


class DeviceDecision(DTO):
    approve: bool = True
    spaceId: str | None = None
    scopes: list[Literal["read", "execute", "manage"]] = Field(default_factory=lambda: ["read", "execute"])


class Execution(DTO):
    actionId: str = Field(min_length=8, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    state: Literal["doing", "review"]
    description: str | None = Field(default=None, max_length=100000)
    ifVersion: str | None = Field(default=None, min_length=1, max_length=128)
    dryRun: bool = False


class Preferences(DTO):
    notifications: bool = True
    timezone: str = Field(default="local", max_length=100)
    name: str = Field(default="", max_length=100)
    onboarded: bool = False
    mascot: Literal["cocoa", "fluent", "noto"] = "cocoa"
    compact: bool = False


class Ack(DTO):
    eventIds: list[str] = Field(min_length=1, max_length=100)


class EmailCodeRequest(DTO):
    email: str = Field(min_length=3, max_length=320)

    @model_validator(mode="after")
    def valid_email(self):
        from .account_auth import normalize_email
        try:
            self.email = normalize_email(self.email)
        except ValueError as exc:
            raise ValueError("邮箱格式不正确") from exc
        return self


class EmailVerifyRequest(EmailCodeRequest):
    code: str = Field(pattern=r"^\d{6}$")


class ExchangeRequest(DTO):
    code: str = Field(min_length=16, max_length=256)


class LinkVikunjaRequest(DTO):
    token: str = Field(min_length=8, max_length=512)


class AdminUserUpdate(DTO):
    """管理后台修改账号（ADMIN-CONSOLE-01）：只允许这三类字段。"""

    name: str | None = Field(default=None, max_length=80)
    role: Literal["superadmin", "admin", "user"] | None = None
    status: Literal["active", "disabled"] | None = None
