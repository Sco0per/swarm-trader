"""Remove insecure database-backed API-key storage.

Revision ID: e7c3a42d1190
Revises: d5e78f9a1b2c
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e7c3a42d1190"
down_revision: Union[str, None] = "d5e78f9a1b2c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Delete legacy plaintext credentials and their storage table."""

    inspector = sa.inspect(op.get_bind())
    if "api_keys" in inspector.get_table_names():
        op.drop_table("api_keys")


def downgrade() -> None:
    """Do not recreate insecure secret storage."""
