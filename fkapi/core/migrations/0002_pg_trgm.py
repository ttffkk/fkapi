"""Enable the pg_trgm extension.

The club/kit/brand search endpoints use trigram similarity (the `%>` / `%`
operators), which requires PostgreSQL's pg_trgm extension. Upstream never
created it, so /api/clubs/search 500s with
"operator does not exist: character varying %> unknown" on a fresh DB.

TrigramExtension runs CREATE EXTENSION IF NOT EXISTS pg_trgm; the fkapi DB
role is a superuser (POSTGRES_USER on the official postgres image), so it has
the privilege.
"""

from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0001_initial"),
    ]

    operations = [
        TrigramExtension(),
    ]
