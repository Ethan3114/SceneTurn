from alembic import op
import sqlalchemy as sa


revision = "0012_project_transfers"
down_revision = "0011_media_candidates"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("transfer_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), index=True),
        sa.Column("kind", sa.String(20), nullable=False), sa.Column("operation_id", sa.String(100), nullable=False),
        sa.Column("identity_sha256", sa.String(64), nullable=False), sa.Column("source_digest", sa.String(64), index=True),
        sa.Column("source_revision", sa.Integer), sa.Column("status", sa.String(30), nullable=False, index=True),
        sa.Column("request_json", sa.JSON, nullable=False), sa.Column("private_json", sa.JSON, nullable=False),
        sa.Column("result_json", sa.JSON, nullable=False), sa.Column("confirmation_hash", sa.String(64)),
        sa.Column("error_code", sa.String(100)), sa.Column("error_message", sa.Text),
        sa.Column("lease_owner", sa.String(100)), sa.Column("lease_expires_at", sa.Float),
        sa.Column("created_at", sa.Float, nullable=False), sa.Column("updated_at", sa.Float, nullable=False),
        sa.UniqueConstraint("owner_id", "operation_id"), sa.UniqueConstraint("owner_id", "kind", "identity_sha256"))


def downgrade():
    op.drop_table("transfer_jobs")
