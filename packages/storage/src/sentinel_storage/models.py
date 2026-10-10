"""SQLAlchemy ORM models (design spec section 12).

Conventions: UTC ``timestamptz`` everywhere, JSONB only for bounded metadata,
no raw secrets and no unbounded raw log payloads.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSONB, list[Any]: JSONB}


def _now() -> Any:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Service(Base):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    namespace: Mapped[str] = mapped_column(String(128))
    owner: Mapped[str] = mapped_column(String(128))
    criticality: Mapped[str] = mapped_column(String(16))
    description: Mapped[str] = mapped_column(Text, default="")
    dependencies: Mapped[list[Any]] = mapped_column(default=list)
    created_at: Mapped[datetime] = _now()


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fingerprint: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(16))
    service_id: Mapped[int | None] = mapped_column(ForeignKey("services.id"))
    service_name: Mapped[str] = mapped_column(String(128))
    environment: Mapped[str] = mapped_column(String(128))
    alert_active: Mapped[bool] = mapped_column(default=True)
    alert_count: Mapped[int] = mapped_column(Integer, default=1)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    service: Mapped[Service | None] = relationship(lazy="raise")

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012 - SQLAlchemy API
    __table_args__ = (
        # At most one non-resolved incident per correlation key, even under races.
        Index(
            "uq_incidents_open_fingerprint",
            "fingerprint",
            unique=True,
            postgresql_where=text("status <> 'RESOLVED'"),
        ),
        Index("ix_incidents_status_first_seen", "status", "first_seen"),
        Index("ix_incidents_service_name_first_seen", "service_name", "first_seen"),
        Index("ix_incidents_fingerprint_resolved_at", "fingerprint", "resolved_at"),
    )


class AlertEventRow(Base):
    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), unique=True)
    incident_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("incidents.id"), index=True)
    fingerprint: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict[str, Any]] = mapped_column()
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class IncidentTransitionRow(Base):
    __tablename__ = "incident_transitions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id"), index=True)
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(Text)
    correlation_id: Mapped[str] = mapped_column(String(128))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuditEvent(Base):
    """Append-only audit log with a hash chain for tamper evidence."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    actor: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(128))
    resource_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str] = mapped_column(String(128), index=True)
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64), unique=True)


class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    topic: Mapped[str] = mapped_column(String(128))
    key: Mapped[str] = mapped_column(String(256))
    payload: Mapped[dict[str, Any]] = mapped_column()
    headers: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = _now()
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_outbox_events_unpublished", "id", postgresql_where=text("published_at IS NULL")),
    )


class ProcessedEvent(Base):
    """Consumer-side idempotency ledger: (consumer, event_id) processed once."""

    __tablename__ = "processed_events"

    consumer: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    processed_at: Mapped[datetime] = _now()


# ---------------------------------------------------------------- Phase 3
class InvestigationRow(Base):
    __tablename__ = "investigations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id"), index=True)
    status: Mapped[str] = mapped_column(String(16))  # running|proposed|escalated|abstained|failed
    provider: Mapped[str] = mapped_column(String(64))
    trigger: Mapped[str] = mapped_column(String(128))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[str] = mapped_column(Text, default="")
    triage: Mapped[dict[str, Any]] = mapped_column(default=dict)
    steps: Mapped[list[Any]] = mapped_column(default=list)
    proposal: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    usage: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index(
            "uq_investigations_running_incident",
            "incident_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
    )


class EvidenceRow(Base):
    __tablename__ = "evidence"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # immutable evidence_id
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id"), index=True)
    investigation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("investigations.id"), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    service: Mapped[str] = mapped_column(String(128))
    source: Mapped[str] = mapped_column(String(32))
    tool: Mapped[str] = mapped_column(String(64))
    query_ref: Mapped[str] = mapped_column(Text)
    range_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    range_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(Text)
    anomalous: Mapped[bool] = mapped_column()
    signals: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


class HypothesisRow(Base):
    __tablename__ = "hypotheses"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id"), index=True)
    investigation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("investigations.id"), index=True)
    key: Mapped[str] = mapped_column(String(16))
    rank: Mapped[int] = mapped_column(Integer)
    cause_category: Mapped[str] = mapped_column(String(64))
    suspected_service: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column()
    adjusted_confidence: Mapped[float] = mapped_column()
    verdict: Mapped[str] = mapped_column(String(16))
    critic_reasons: Mapped[list[Any]] = mapped_column(default=list)
    evidence_refs: Mapped[list[Any]] = mapped_column(default=list)
    counterevidence_refs: Mapped[list[Any]] = mapped_column(default=list)
    recommended_runbook_id: Mapped[str | None] = mapped_column(String(64))
    runbook_params: Mapped[dict[str, Any]] = mapped_column(default=dict)
    uncertainty: Mapped[str] = mapped_column(Text, default="")
    next_checks: Mapped[list[Any]] = mapped_column(default=list)
    created_at: Mapped[datetime] = _now()


class IdempotencyKey(Base):
    """Stored responses for mutating API calls keyed by (principal, Idempotency-Key)."""

    __tablename__ = "idempotency_keys"

    principal: Mapped[str] = mapped_column(String(128), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    route: Mapped[str] = mapped_column(String(256))
    request_hash: Mapped[str] = mapped_column(String(64))
    status_code: Mapped[int] = mapped_column(Integer)
    response: Mapped[dict[str, Any]] = mapped_column()
    created_at: Mapped[datetime] = _now()
