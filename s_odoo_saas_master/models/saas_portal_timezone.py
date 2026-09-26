import logging

import pytz

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class SaasOdooInstanceTimezone(models.Model):
    """Rendering helper so every portal date follows the customer's timezone."""

    _inherit = 'saas.odoo.instance'

    def dt(self, value, fmt='%d %b %Y, %H:%M'):
        """Format a stored (UTC) datetime in the *current user's* timezone.

        Odoo stores every datetime in UTC; the portal used to print those raw values, so a
        customer in UTC+11 saw backups, history, logs and expiry dates 11 hours off.
        """
        if not value:
            return ''
        try:
            return fields.Datetime.context_timestamp(self.env.user, value).strftime(fmt)
        except Exception as error:  # pragma: no cover - never break a page for a date
            _logger.warning('Could not format %s in the user timezone: %s', value, error)
            return value.strftime(fmt) if hasattr(value, 'strftime') else str(value)

    @api.model
    def saas_available_timezones(self):
        """Timezone choices for the portal selector (common zones, UTC first)."""
        zones = list(pytz.common_timezones)
        if 'UTC' in zones:
            zones.remove('UTC')
        return ['UTC'] + zones
