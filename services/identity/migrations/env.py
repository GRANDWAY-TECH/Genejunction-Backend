from app import models  # noqa: F401  (registers tables on the shared metadata)
from app.config import settings
from gj_common.alembic_env import run

run(settings.database_url)
