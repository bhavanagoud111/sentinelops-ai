"""Phase 4: runbooks, approvals, executions.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)
J = pg.JSONB


def upgrade() -> None:
    from sentinel_policy import CATALOG

    runbooks = op.create_table(
        "runbooks",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("action_type", sa.String(64), nullable=False),
        sa.Column("title", sa.String(128), nullable=False),
        sa.Column("allowed_scopes", J, nullable=False),
        sa.Column("parameter_schema", J, nullable=False),
        sa.Column("risk_level", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_runbooks"),
    )
    op.bulk_insert(
        runbooks,
        [
            {
                "id": rb.id.value,
                "action_type": rb.id.value,
                "title": rb.title,
                "allowed_scopes": {
                    "namespaces": sorted(rb.allowed_namespaces),
                    "deployments": sorted(rb.allowed_deployments),
                },
                "parameter_schema": rb.params_model.model_json_schema(),
                "risk_level": rb.risk.value,
                "version": rb.version,
            }
            for rb in CATALOG.values()
        ],
    )

    op.create_table(
        "approvals",
        sa.Column("id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("incident_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("investigation_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("runbook_id", sa.String(64), nullable=False),
        sa.Column("params", J, nullable=False),
        sa.Column("rationale", sa.Text, nullable=False),
        sa.Column("risk", sa.String(16), nullable=False),
        sa.Column("target_revision", sa.Integer, nullable=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("requested_by", sa.String(128), nullable=False),
        sa.Column("requested_at", TS, nullable=False),
        sa.Column("approved_by", sa.String(128), nullable=True),
        sa.Column("decided_at", TS, nullable=True),
        sa.Column("decision_reason", sa.Text, nullable=True),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_approvals"),
        sa.ForeignKeyConstraint(
            ["incident_id"], ["incidents.id"], name="fk_approvals_incident_id_incidents"
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["investigations.id"],
            name="fk_approvals_investigation_id_investigations",
        ),
        sa.CheckConstraint(
            "status IN ('pending','approved','rejected','expired','consumed')",
            name="ck_approvals_status_valid",
        ),
        sa.CheckConstraint(
            "approved_by IS NULL OR approved_by <> requested_by", name="ck_approvals_four_eyes"
        ),
    )
    op.create_index("ix_approvals_incident_id", "approvals", ["incident_id"])
    op.create_index(
        "uq_approvals_open_incident",
        "approvals",
        ["incident_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'approved')"),
    )

    op.create_table(
        "executions",
        sa.Column("id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("incident_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("approval_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("runbook_id", sa.String(64), nullable=False),
        sa.Column("target", sa.String(256), nullable=False),
        sa.Column("params", J, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("requested_by", sa.String(128), nullable=False),
        sa.Column("policy", J, nullable=False),
        sa.Column("command_template", sa.Text, nullable=False),
        sa.Column("before_state", J, nullable=False),
        sa.Column("after_state", J, nullable=False),
        sa.Column("verification", J, nullable=False),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", TS, nullable=True),
        sa.Column("finished_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_executions"),
        sa.ForeignKeyConstraint(
            ["incident_id"], ["incidents.id"], name="fk_executions_incident_id_incidents"
        ),
        sa.ForeignKeyConstraint(
            ["approval_id"], ["approvals.id"], name="fk_executions_approval_id_approvals"
        ),
        sa.UniqueConstraint("approval_id", name="uq_executions_approval_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_executions_idempotency_key"),
        sa.CheckConstraint(
            "status IN ('pending','running','succeeded','failed','blocked')",
            name="ck_executions_status_valid",
        ),
    )
    op.create_index("ix_executions_incident_id", "executions", ["incident_id"])
    op.create_index(
        "uq_executions_active_target",
        "executions",
        ["target"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )


def downgrade() -> None:
    op.drop_table("executions")
    op.drop_table("approvals")
    op.drop_table("runbooks")
