"""The start of the public chain of migrations.

A database this app makes is built from the models and stamped at the newest revision, so the migrations before this
one never run on it. This revision stands in for all of them and changes nothing; the migrations after it follow
unchanged.

Revision ID: 0170
Revises:
"""

revision = "0170"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    # Forward only.
    pass
