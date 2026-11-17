"""Moving the data from the source database (Postgres) to SQLite, once: `resumebot copy-to-sqlite` (`copier.copy`).

It exists for moving an existing Postgres database to SQLite and goes with Postgres.
It only reads the old database: nothing here writes to it.
"""
