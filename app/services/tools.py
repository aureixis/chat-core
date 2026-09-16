from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import BotAction, RobotToolGrant, ToolDefinition, ToolInvocation


class ToolAuthorizationError(ValueError):
    pass


def request_tool_invocation(
    database: Session,
    bot_id: int,
    tool_name: str,
    input_json: dict,
    *,
    action_id: int | None = None,
    requested_by_id: int | None = None,
) -> ToolInvocation:
    if action_id is not None:
        action = database.get(BotAction, action_id)
        if not action or action.bot_id != bot_id:
            raise ToolAuthorizationError("Tool action is unavailable or out of scope")
    tool = database.scalar(
        select(ToolDefinition).where(
            ToolDefinition.name == tool_name, ToolDefinition.enabled.is_(True)
        )
    )
    if not tool:
        raise ToolAuthorizationError("Tool is unavailable")
    grant = database.scalar(
        select(RobotToolGrant).where(
            RobotToolGrant.bot_id == bot_id,
            RobotToolGrant.tool_id == tool.id,
            RobotToolGrant.allowed.is_(True),
        )
    )
    if not grant:
        raise ToolAuthorizationError("Robot is not allowed to use this tool")
    _validate_input(tool.input_schema or {}, input_json)
    _validate_constraints(grant.constraints_json or {}, input_json)
    now = datetime.now(timezone.utc)
    invocation = ToolInvocation(
        bot_id=bot_id,
        organization_id=grant.organization_id,
        tool_id=tool.id,
        action_id=action_id,
        requested_by_id=requested_by_id,
        capability_id=uuid4().hex,
        status=(
            "approval_required"
            if grant.requires_approval or tool.risk_level in {"high", "critical"}
            else "authorized"
        ),
        input_json=input_json,
        expires_at=now + timedelta(minutes=5),
    )
    database.add(invocation)
    database.flush()
    return invocation


def approve_tool_invocation(database: Session, invocation: ToolInvocation, approver_id: int) -> str:
    now = datetime.now(timezone.utc)
    expires_at = invocation.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if invocation.status != "approval_required" or expires_at <= now:
        raise ToolAuthorizationError("Tool invocation cannot be approved")
    invocation.status = "authorized"
    invocation.approved_by_id = approver_id
    return issue_capability_token(invocation)


def issue_capability_token(invocation: ToolInvocation) -> str:
    if invocation.status != "authorized":
        raise ToolAuthorizationError("Tool invocation is not authorized")
    return jwt.encode(
        {
            "sub": str(invocation.bot_id),
            "type": "tool_capability",
            "jti": invocation.capability_id,
            "invocation_id": invocation.id,
            "tool_id": invocation.tool_id,
            "exp": invocation.expires_at,
            "iss": get_settings().jwt_issuer,
        },
        get_settings().jwt_secret,
        algorithm="HS256",
    )


def consume_capability_token(database: Session, token: str) -> ToolInvocation:
    try:
        payload = jwt.decode(
            token,
            get_settings().jwt_secret,
            algorithms=["HS256"],
            issuer=get_settings().jwt_issuer,
        )
        if payload.get("type") != "tool_capability":
            raise ValueError("Wrong token type")
        invocation_id = int(payload["invocation_id"])
    except (jwt.PyJWTError, KeyError, TypeError, ValueError) as error:
        raise ToolAuthorizationError("Invalid tool capability") from error
    query = select(ToolInvocation).where(ToolInvocation.id == invocation_id)
    if database.bind and database.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    invocation = database.scalar(query)
    now = datetime.now(timezone.utc)
    expires_at = invocation.expires_at if invocation else now
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if (
        not invocation
        or invocation.status != "authorized"
        or invocation.capability_id != payload.get("jti")
        or invocation.bot_id != int(payload.get("sub", 0))
        or invocation.tool_id != int(payload.get("tool_id", 0))
        or expires_at <= now
    ):
        raise ToolAuthorizationError("Tool capability is expired, consumed, or out of scope")
    tool_enabled = database.scalar(
        select(ToolDefinition.enabled).where(ToolDefinition.id == invocation.tool_id)
    )
    grant_allowed = database.scalar(
        select(RobotToolGrant.allowed).where(
            RobotToolGrant.bot_id == invocation.bot_id,
            RobotToolGrant.tool_id == invocation.tool_id,
        )
    )
    if not tool_enabled or not grant_allowed:
        invocation.status = "revoked"
        raise ToolAuthorizationError("Tool capability was revoked")
    invocation.status = "executing"
    return invocation


def complete_tool_invocation(
    invocation: ToolInvocation, result_json: dict, *, succeeded: bool
) -> None:
    if invocation.status != "executing":
        raise ToolAuthorizationError("Tool invocation is not executing")
    invocation.result_json = result_json
    invocation.status = "completed" if succeeded else "failed"
    invocation.completed_at = datetime.now(timezone.utc)


def expire_tool_invocations(database: Session) -> int:
    result = database.execute(
        update(ToolInvocation)
        .where(
            ToolInvocation.status.in_(["requested", "approval_required", "authorized"]),
            ToolInvocation.expires_at < datetime.now(timezone.utc),
        )
        .values(status="expired")
    )
    return int(result.rowcount or 0)


def _validate_input(schema: dict, value: dict) -> None:
    if schema.get("type", "object") != "object":
        raise ToolAuthorizationError("Only object tool schemas are supported")
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    missing = required - set(value)
    if missing:
        raise ToolAuthorizationError(f"Missing tool inputs: {', '.join(sorted(missing))}")
    if schema.get("additionalProperties") is False:
        unexpected = set(value) - set(properties)
        if unexpected:
            raise ToolAuthorizationError(f"Unexpected tool inputs: {', '.join(sorted(unexpected))}")
    for name, item in value.items():
        expected = (properties.get(name) or {}).get("type")
        valid = {
            "string": isinstance(item, str),
            "integer": isinstance(item, int) and not isinstance(item, bool),
            "number": isinstance(item, (int, float)) and not isinstance(item, bool),
            "boolean": isinstance(item, bool),
            "object": isinstance(item, dict),
            "array": isinstance(item, list),
            None: True,
        }.get(expected, False)
        if not valid:
            raise ToolAuthorizationError(f"Invalid type for tool input {name}")


def _validate_constraints(constraints: dict, value: dict) -> None:
    max_bytes = int(constraints.get("max_input_bytes", 16_384))
    if len(str(value).encode("utf-8")) > max_bytes:
        raise ToolAuthorizationError("Tool input exceeds the granted size limit")
    allowed_values = constraints.get("allowed_values") or {}
    blocked_values = constraints.get("blocked_values") or {}
    for field, allowed in allowed_values.items():
        if field in value and value[field] not in allowed:
            raise ToolAuthorizationError(f"Tool input {field} is outside the granted scope")
    for field, blocked in blocked_values.items():
        if field in value and value[field] in blocked:
            raise ToolAuthorizationError(f"Tool input {field} is blocked by the grant")
