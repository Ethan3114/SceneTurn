from alembic import op
import sqlalchemy as sa


revision = "0015_model_provider_registry"
down_revision = "0014_codex_image_jobs"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("model_provider_connections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("scope_key", sa.String(50), nullable=False, index=True),
        sa.Column("provider_id", sa.String(50), nullable=False),
        sa.Column("api_key_encrypted", sa.Text),
        sa.Column("config_json", sa.JSON, nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
        sa.UniqueConstraint("scope_key", "provider_id"))


def downgrade():
    op.drop_table("model_provider_connections")
