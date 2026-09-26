# Backfill the new ``version_type`` field (Community/Enterprise) on servers and instances.
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})

    # Any pre-existing record keeps the previous behaviour (Community).
    cr.execute("UPDATE saas_odoo_server SET version_type = 'community' WHERE version_type IS NULL")
    cr.execute("UPDATE saas_odoo_instance SET version_type = 'community' WHERE version_type IS NULL")

    # Align each instance with the server it is already deployed on, without swapping servers.
    for instance in env['saas.odoo.instance'].search([('odoo_server_id', '!=', False)]):
        server = instance.odoo_server_id
        if server.version_type and instance.version_type != server.version_type:
            instance.write({
                'version_type': server.version_type,
                'odoo_server_id': server.id,
            })

    _logger.info("version_type backfilled on saas.odoo.server and saas.odoo.instance")
