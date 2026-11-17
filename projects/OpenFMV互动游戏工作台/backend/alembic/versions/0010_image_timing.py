from alembic import op
import sqlalchemy as sa


revision = "0010_image_timing"
down_revision = "0009_image_queue"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("image_jobs", sa.Column("submitted_at", sa.Float))
    op.add_column("image_jobs", sa.Column("finished_at", sa.Float))


def downgrade():
    op.drop_column("image_jobs", "finished_at")
    op.drop_column("image_jobs", "submitted_at")
