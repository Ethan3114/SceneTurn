from alembic import op
import sqlalchemy as sa


revision = "0013_public_release"
down_revision = "0012_project_transfers"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "publications",
        sa.Column("slug", sa.String(36), primary_key=True),
        sa.Column("transfer_id", sa.String(36), sa.ForeignKey("transfer_jobs.id"), nullable=False, unique=True),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("poster_asset_id", sa.String(36), sa.ForeignKey("assets.id")),
        sa.Column("trailer_asset_id", sa.String(36), sa.ForeignKey("assets.id")),
        sa.Column("published", sa.Boolean(), nullable=False, index=True),
        sa.Column("created_at", sa.Float(), nullable=False),
    )


def downgrade():
    op.drop_table("publications")
