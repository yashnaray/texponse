"""SMS suppression list (TCPA/CTIA compliance).

Revision ID: 0003_sms_suppression
Revises: 0002_roles_companies
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_sms_suppression"
down_revision = "0002_roles_companies"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sms_suppressions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("company_id", sa.String(), nullable=False, index=True),
        sa.Column("phone_hash", sa.String(), nullable=False, index=True),
        sa.Column("status", sa.String(), nullable=False, index=True),
        sa.Column("keyword", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("company_id", "phone_hash", name="uq_sms_suppression_company_hash"),
    )


def downgrade() -> None:
    op.drop_table("sms_suppressions")
