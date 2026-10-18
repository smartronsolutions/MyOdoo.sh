"""Storage is now live — drop the hourly storage-limit cron.

The portal recomputes the storage values whenever the instance page is opened
(and right after a backup / image change), so the hourly
``cron_check_storage_limits`` scan is no longer needed. The XML record was removed
from ``data/ir_cron_data.xml``; this deletes the record that is still in the DB.
"""
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)

OLD_CRON_NAMES = (
    'SaaS Master: Check instance storage limits',
)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Cron = env['ir.cron'].sudo()
    cron = env.ref('s_odoo_saas_master.cron_saas_check_storage_limits', raise_if_not_found=False)
    crones = cron | Cron.search([('cron_name', 'in', list(OLD_CRON_NAMES))])
    if crones:
        _logger.info("Removing storage-limit cron(s): %s", crones.mapped('cron_name'))
        crones.unlink()
