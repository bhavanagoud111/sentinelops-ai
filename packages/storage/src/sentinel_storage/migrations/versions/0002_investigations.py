"""Phase 3: investigations, evidence, hypotheses, idempotency keys.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)
J = pg.JSONB


def upgrade() -> None:
    op.create_table(
        "investigations",
        sa.Column("id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("incident_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("trigger", sa.String(128), nullable=False),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("finished_at", TS, nullable=True),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("triage", J, nullable=False),
        sa.Column("steps", J, nullable=False),
        sa.Column("proposal", J, nullable=True),
        sa.Column("usage", J, nullable=False),
        sa.Column("error", sa.Text, nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_investigations"),
        sa.ForeignKeyConstraint(
            ["incident_id"], ["incidents.id"], name="fk_investigations_incident_id_incidents"
        ),
    )
    op.create_index("ix_investigations_incident_id", "investigations", ["incident_id"])
    op.create_index(
        "uq_investigations_running_incident",
        "investigations",
        ["incident_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )

    op.create_table(
        "evidence",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("incident_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("investigation_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("service", sa.String(128), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("tool", sa.String(64), nullable=False),
        sa.Column("query_ref", sa.Text, nullable=False),
        sa.Column("range_start", TS, nullable=False),
        sa.Column("range_end", TS, nullable=False),
        sa.Column("retrieved_at", TS, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("anomalous", sa.Boolean, nullable=False),
        sa.Column("signals", J, nullable=False),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_evidence"),
        sa.ForeignKeyConstraint(
            ["incident_id"], ["incidents.id"], name="fk_evidence_incident_id_incidents"
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["investigations.id"],
            name="fk_evidence_investigation_id_investigations",
        ),
    )
    op.create_index("ix_evidence_incident_id", "evidence", ["incident_id"])
    op.create_index("ix_evidence_investigation_id", "evidence", ["investigation_id"])

    op.create_table(
        "hypotheses",
        sa.Column("id", sa.BigInteger, nullable=False),
        sa.Column("incident_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("investigation_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.String(16), nullable=False),
        sa.Column("rank", sa.Integer, nullable=False),
        sa.Column("cause_category", sa.String(64), nullable=False),
        sa.Column("suspected_service", sa.String(128), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("adjusted_confidence", sa.Float, nullable=False),
        sa.Column("verdict", sa.String(16), nullable=False),
        sa.Column("critic_reasons", J, nullable=False),
        sa.Column("evidence_refs", J, nullable=False),
        sa.Column("counterevidence_refs", J, nullable=False),
        sa.Column("recommended_runbook_id", sa.String(64), nullable=True),
        sa.Column("runbook_params", J, nullable=False),
        sa.Column("uncertainty", sa.Text, nullable=False),
        sa.Column("next_checks", J, nullable=False),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_hypotheses"),
        sa.ForeignKeyConstraint(
            ["incident_id"], ["incidents.id"], name="fk_hypotheses_incident_id_incidents"
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["investigations.id"],
            name="fk_hypotheses_investigation_id_investigations",
        ),
    )
    op.create_index("ix_hypotheses_incident_id", "hypotheses", ["incident_id"])
    op.create_index("ix_hypotheses_investigation_id", "hypotheses", ["investigation_id"])

    op.create_table(
        "idempotency_keys",
        sa.Column("principal", sa.String(128), nullable=False),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("route", sa.String(256), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status_code", sa.Integer, nullable=False),
        sa.Column("response", J, nullable=False),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("principal", "key", name="pk_idempotency_keys"),
    )


def downgrade() -> None:
    op.drop_table("idempotency_keys")
    op.drop_table("hypotheses")
    op.drop_table("evidence")
    op.drop_table("investigations")
