from django.db import models

from olympia.amo.fields import PositiveAutoField


class TestRegularCharField(models.Model):
    name = models.CharField(max_length=255)


class TestPositiveAutoFieldModel(models.Model):
    """Dedicated table for the PositiveAutoField tests.

    Those tests insert the unsigned-int maximum as a primary key, and InnoDB
    does not roll the AUTO_INCREMENT counter back when the test transaction is
    rolled back. Every later insert into that table is then handed the clamped
    maximum and collides. Keeping it in a table nothing else uses means the
    damage stays here instead of breaking unrelated tests that create rows in
    a shared table.
    """

    id = PositiveAutoField(primary_key=True)
