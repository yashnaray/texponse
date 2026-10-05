"""Roles (user/superuser/dev) + companies table.

Revision ID: 0002_roles_companies
Revises: 0001_initial
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_roles_companies"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "companies",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("name", sa.String(), nullable=False, index=True),
        sa.Column("jobber_company_id", sa.String(), unique=True, index=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
    )
    # Backfill: every distinct users.company_id becomes a company row.
    op.execute(sa.text(
        "INSERT INTO companies (id, name, created_at, updated_at) "
        "SELECT DISTINCT company_id, company_id, now(), now() FROM users "
        "WHERE company_id IS NOT NULL AND company_id NOT IN (SELECT id FROM companies)"
    ))
    # Normalize legacy default 'viewer' -> 'user'.
    op.execute(sa.text("UPDATE users SET role='user' WHERE role='viewer'"))
    # Allow devs/superusers without a company (global access).
    with op.batch_alter_table("users") as batch:
        batch.alter_column("company_id", existing_type=sa.String(), nullable=True)


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.alter_column("company_id", existing_type=sa.String(), nullable=False)
    op.drop_table("companies")
