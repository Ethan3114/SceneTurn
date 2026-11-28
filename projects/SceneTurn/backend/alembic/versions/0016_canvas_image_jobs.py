from alembic import op
import sqlalchemy as sa


revision = "0016_canvas_image_jobs"
down_revision = "0015_model_provider_registry"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("image_jobs") as table:
        table.alter_column("run_id", existing_type=sa.String(36), nullable=True)


def downgrade():
    with op.batch_alter_table("image_jobs") as table:
        table.alter_column("run_id", existing_type=sa.String(36), nullable=False)
