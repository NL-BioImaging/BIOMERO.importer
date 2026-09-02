"""add import options to imports

Revision ID: 92e8f9e637a1
Revises: 78d3d6d84aec
Create Date: 2026-08-19
"""
from alembic import op
import sqlalchemy as sa


revision = "92e8f9e637a1"
down_revision = "78d3d6d84aec"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "imports",
        sa.Column("import_options", sa.Text(), nullable=True),
    )


def downgrade():
    op.drop_column("imports", "import_options")
