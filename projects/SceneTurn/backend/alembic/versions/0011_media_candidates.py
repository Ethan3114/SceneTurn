from alembic import op
import sqlalchemy as sa


revision = "0011_media_candidates"
down_revision = "0010_image_timing"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("media_sequences",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False, index=True),
        sa.Column("scene_id", sa.String(100), nullable=False), sa.Column("input_json", sa.JSON, nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False), sa.Column("created_at", sa.Float, nullable=False))
    op.create_table("media_adoptions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False, index=True),
        sa.Column("scene_id", sa.String(100), nullable=False),
        sa.Column("asset_id", sa.String(36), sa.ForeignKey("assets.id"), nullable=False),
        sa.Column("generation_id", sa.String(36), sa.ForeignKey("generations.id")),
        sa.Column("clip_id", sa.String(100), nullable=False), sa.Column("operation_id", sa.String(100), nullable=False),
        sa.Column("project_revision", sa.Integer, nullable=False), sa.Column("target_json", sa.JSON),
        sa.Column("proposal_json", sa.JSON, nullable=False), sa.Column("confirmation_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, index=True), sa.Column("applied_revision", sa.Integer),
        sa.Column("created_at", sa.Float, nullable=False), sa.Column("updated_at", sa.Float, nullable=False),
        sa.UniqueConstraint("owner_id", "project_id", "operation_id"))


def downgrade():
    op.drop_table("media_adoptions")
    op.drop_table("media_sequences")
