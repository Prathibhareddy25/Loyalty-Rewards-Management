import os


os.environ["DATABASE_URL"] = "sqlite://"

from app import app, db

with app.app_context():
    if db.engine.url.get_backend_name() != "sqlite":
        raise RuntimeError("The test suite must use an isolated SQLite database.")
