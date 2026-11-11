"""owners for broker accounts and strategy instances (partners)

Revision ID: 0002
Revises: 0001
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # plain ADD COLUMN (no table rebuild): a SQLite batch rebuild of broker_accounts fails with
    # foreign_keys=ON because trades / orders / strategies point at it. No DB-level foreign key either
    # (SQLite cannot add one without that rebuild); the app checks ownership itself.
    for table in ("broker_accounts", "strategy_instances"):
        op.add_column(table, sa.Column("owner_user_id", sa.Integer(), nullable=True))
        op.create_index(f"ix_{table}_owner_user_id", table, ["owner_user_id"], unique=False)
    # everything that exists today belongs to the first admin
    conn = op.get_bind()
    admin = conn.execute(sa.text("SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1")).scalar()
    if admin is not None:
        for table in ("broker_accounts", "strategy_instances"):
            conn.execute(sa.text(f"UPDATE {table} SET owner_user_id = :a WHERE owner_user_id IS NULL"), {"a": admin})


def downgrade() -> None:
    for table in ("broker_accounts", "strategy_instances"):
        op.drop_index(f"ix_{table}_owner_user_id", table_name=table)
        op.drop_column(table, "owner_user_id")
