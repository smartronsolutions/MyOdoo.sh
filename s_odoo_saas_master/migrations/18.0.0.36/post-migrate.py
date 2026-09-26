# Create the instance-backup folders at upgrade time too, so an already-installed server
# gets them without waiting for the first backup.
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    directories = env['saas.odoo.instance']._ensure_backup_directories()
    _logger.info("Instance backup directories ready: %s", directories)
