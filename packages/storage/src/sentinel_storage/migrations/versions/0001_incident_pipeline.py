"""Phase 2: incident pipeline tables and service catalog seed.

Revision ID: 0001
Revises:
Create Date: 2026-10-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    services = op.create_table(
        "services",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("namespace", sa.String(128), nullable=False),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("criticality", sa.String(16), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("dependencies", pg.JSONB, nullable=False),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_services"),
        sa.UniqueConstraint("name", name="uq_services_name"),
    )

    op.create_table(
        "incidents",
        sa.Column("id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("fingerprint", sa.String(128), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("service_id", sa.Integer, nullable=True),
        sa.Column("service_name", sa.String(128), nullable=False),
        sa.Column("environment", sa.String(128), nullable=False),
        sa.Column("alert_active", sa.Boolean, nullable=False),
        sa.Column("alert_count", sa.Integer, nullable=False),
        sa.Column("first_seen", TS, nullable=False),
        sa.Column("last_seen", TS, nullable=False),
        sa.Column("resolved_at", TS, nullable=True),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_incidents"),
        sa.ForeignKeyConstraint(
            ["service_id"], ["services.id"], name="fk_incidents_service_id_services"
        ),
        sa.CheckConstraint(
            "status IN ('OPEN','TRIAGING','INVESTIGATING','AWAITING_APPROVAL','REMEDIATING',"
            "'VERIFYING','RESOLVED','NEEDS_HUMAN','REJECTED','FAILED')",
            name="ck_incidents_status_valid",
        ),
    )
    op.create_index(
        "uq_incidents_open_fingerprint",
        "incidents",
        ["fingerprint"],
        unique=True,
        postgresql_where=sa.text("status <> 'RESOLVED'"),
    )
    op.create_index("ix_incidents_status_first_seen", "incidents", ["status", "first_seen"])
    op.create_index(
        "ix_incidents_service_name_first_seen", "incidents", ["service_name", "first_seen"]
    )
    op.create_index(
        "ix_incidents_fingerprint_resolved_at", "incidents", ["fingerprint", "resolved_at"]
    )

    op.create_table(
        "alert_events",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("event_id", sa.String(128), nullable=False),
        sa.Column("incident_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("fingerprint", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("payload", pg.JSONB, nullable=False),
        sa.Column("received_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_alert_events"),
        sa.UniqueConstraint("event_id", name="uq_alert_events_event_id"),
        sa.ForeignKeyConstraint(
            ["incident_id"], ["incidents.id"], name="fk_alert_events_incident_id_incidents"
        ),
    )
    op.create_index("ix_alert_events_incident_id", "alert_events", ["incident_id"])
    op.create_index("ix_alert_events_fingerprint", "alert_events", ["fingerprint"])

    op.create_table(
        "incident_transitions",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("incident_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("from_status", sa.String(32), nullable=True),
        sa.Column("to_status", sa.String(32), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("correlation_id", sa.String(128), nullable=False),
        sa.Column("at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_incident_transitions"),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.id"],
            name="fk_incident_transitions_incident_id_incidents",
        ),
    )
    op.create_index("ix_incident_transitions_incident_id", "incident_transitions", ["incident_id"])

    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.String(128), nullable=False),
        sa.Column("details", pg.JSONB, nullable=False),
        sa.Column("at", TS, nullable=False),
        sa.Column("prev_hash", sa.String(64), nullable=False),
        sa.Column("hash", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
        sa.UniqueConstraint("hash", name="uq_audit_events_hash"),
    )
    op.create_index("ix_audit_events_resource_id", "audit_events", ["resource_id"])
    # Append-only: block UPDATE/DELETE at the database level.
    op.execute(
        """
        CREATE FUNCTION audit_events_immutable() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'audit_events is append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_events_no_update_delete
          BEFORE UPDATE OR DELETE ON audit_events
          FOR EACH ROW EXECUTE FUNCTION audit_events_immutable()
        """
    )

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("topic", sa.String(128), nullable=False),
        sa.Column("key", sa.String(256), nullable=False),
        sa.Column("payload", pg.JSONB, nullable=False),
        sa.Column("headers", pg.JSONB, nullable=False),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("published_at", TS, nullable=True),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_outbox_events"),
    )
    op.create_index(
        "ix_outbox_events_unpublished",
        "outbox_events",
        ["id"],
        postgresql_where=sa.text("published_at IS NULL"),
    )

    op.create_table(
        "processed_events",
        sa.Column("consumer", sa.String(64), nullable=False),
        sa.Column("event_id", sa.String(128), nullable=False),
        sa.Column("processed_at", TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("consumer", "event_id", name="pk_processed_events"),
    )

    ns = "sentinelops-demo"
    op.bulk_insert(
        services,
        [
            {
                "name": "checkout",
                "namespace": ns,
                "owner": "team-payments",
                "criticality": "critical",
                "description": "Order placement API. Calls inventory and payment.",
                "dependencies": ["inventory", "payment"],
            },
            {
                "name": "inventory",
                "namespace": ns,
                "owner": "team-fulfilment",
                "criticality": "high",
                "description": "Stock levels and reservations.",
                "dependencies": [],
            },
            {
                "name": "payment",
                "namespace": ns,
                "owner": "team-payments",
                "criticality": "critical",
                "description": "Payment provider simulator.",
                "dependencies": [],
            },
            {
                "name": "worker",
                "namespace": ns,
                "owner": "team-fulfilment",
                "criticality": "medium",
                "description": "Background order-processing worker.",
                "dependencies": [],
            },
        ],
    )


def downgrade() -> None:
    op.drop_table("processed_events")
    op.drop_index("ix_outbox_events_unpublished", table_name="outbox_events")
    op.drop_table("outbox_events")
    op.execute("DROP TRIGGER IF EXISTS audit_events_no_update_delete ON audit_events")
    op.execute("DROP FUNCTION IF EXISTS audit_events_immutable()")
    op.drop_table("audit_events")
    op.drop_table("incident_transitions")
    op.drop_table("alert_events")
    op.drop_table("incidents")
    op.drop_table("services")
