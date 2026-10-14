import pytest

from ecourse.auth.service import initialize_owner
from ecourse.config import Settings
from ecourse.db import Database
from ecourse.vault import initialize_key


@pytest.fixture
def backend(tmp_path):
    data, secrets = tmp_path / "data", tmp_path / "secrets"
    initialize_key(secrets, data)
    settings = Settings(data, secrets)
    db = Database(settings)
    db.initialize()
    owner = initialize_owner(settings, "owner", "a-long-local-password")
    return settings, db, owner
