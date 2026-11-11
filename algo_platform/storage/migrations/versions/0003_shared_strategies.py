"""shared strategies: rules defined once, run by each partner on their own account

Revision ID: 0003
Revises: 0002
"""
from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

import storage.models

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "shared_strategies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("template_id", sa.String(length=64), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", storage.models.ISTDateTime(), nullable=False),
        sa.Column("updated_at", storage.models.ISTDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index("ix_shared_strategies_created_by", "shared_strategies", ["created_by"], unique=False)
    # plain ADD COLUMN, no table rebuild (see 0002)
    op.add_column("strategy_instances", sa.Column("strategy_id", sa.Integer(), nullable=True))
    op.create_index("ix_strategy_instances_strategy_id", "strategy_instances", ["strategy_id"], unique=False)

    # every existing instance becomes a shared strategy (same name, template, rules) + its run
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, name, template_id, params, notes, owner_user_id, created_at "
                                "FROM strategy_instances ORDER BY id")).fetchall()
    for iid, name, template_id, params, notes, owner, created in rows:
        if not isinstance(params, str):
            params = json.dumps(params or {})
        conn.execute(sa.text(
            "INSERT INTO shared_strategies (name, template_id, params, notes, created_by, created_at, updated_at) "
            "VALUES (:n, :t, :p, :notes, :by, :c, :c)"),
            {"n": name, "t": template_id, "p": params, "notes": notes or "", "by": owner, "c": created})
        sid = conn.execute(sa.text("SELECT id FROM shared_strategies WHERE name = :n"), {"n": name}).scalar()
        conn.execute(sa.text("UPDATE strategy_instances SET strategy_id = :s WHERE id = :i"), {"s": sid, "i": iid})


def downgrade() -> None:
    op.drop_index("ix_strategy_instances_strategy_id", table_name="strategy_instances")
    op.drop_column("strategy_instances", "strategy_id")
    op.drop_index("ix_shared_strategies_created_by", table_name="shared_strategies")
    op.drop_table("shared_strategies")
