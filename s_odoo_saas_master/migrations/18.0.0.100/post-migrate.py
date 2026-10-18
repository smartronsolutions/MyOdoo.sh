"""Backfill the "staging database exists" flag.

The portal now shows "Delete Staging Database" only when there is really a test
copy to delete (``staging_db_exists``). Instances that were already unlocked
through the old "Remove DB Filter & DB Name" flow most likely still have their
test database on the server, so the flag is set for them here instead of
hiding the delete button until the next create.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute(
        """
        UPDATE saas_odoo_instance
           SET staging_db_exists = TRUE
         WHERE conf_staging_mode IS TRUE
           AND (staging_db_exists IS NULL OR staging_db_exists IS FALSE)
        """
    )
    _logger.info(
        "Staging database flag backfilled for %s instance(s).", cr.rowcount)
