# A backup whose background worker died (server restart, PostgreSQL serialization error,
# killed thread, ...) used to stay 'running' forever. Because the portal then always
# shows the "Your backup is in progress" screen, every instance that ever hit the bug was
# left with a spinner that never stopped.
#
# The code now writes a terminal state on a dedicated connection and a watchdog cron
# clears stale rows. This migration unblocks the databases that are already affected.
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    # ``_recover_stuck_backups`` accepts a timeout, but the moment of the upgrade is the
    # right time to clear everything that is still 'running': no worker is alive across
    # an upgrade, so any remaining 'running' row is dead by definition.
    count = env['saas.odoo.instance']._recover_stuck_backups(timeout_minutes=0)
    _logger.info("Backup watchdog: %s stuck backup(s) marked as failed.", count)
