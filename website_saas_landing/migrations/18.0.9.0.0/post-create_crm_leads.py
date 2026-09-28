# Part of Odoo. See LICENSE file for full copyright and licensing details.

import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Create the CRM lead for enquiries submitted before this feature.

    ``_create_crm_lead`` is idempotent, so re-running is safe. Skipped on a
    fresh install (``version`` is ``None``), where there is nothing to backfill.
    """
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    if 'website.saas.enquiry' not in env or 'crm.lead' not in env:
        return
    if 'saas_enquiry_id' not in env['crm.lead']._fields:
        return
    created = 0
    for enquiry in env['website.saas.enquiry'].search([]):
        had_lead = bool(enquiry.crm_lead_ids)
        enquiry._create_crm_lead()
        if not had_lead and enquiry.crm_lead_ids:
            created += 1
    if created:
        _logger.info(
            "Created %s CRM lead(s) for existing website SaaS enquiries.", created
        )

    # Mail templates are noupdate records, so upgrading does not refresh them.
    # Align the sender with the new helper (runtime ``email_values`` already
    # override it, this keeps UI test-sends consistent too).
    for xmlid in (
        'website_saas_landing.mail_template_enquiry_internal',
        'website_saas_landing.mail_template_enquiry_customer',
    ):
        template = env.ref(xmlid, raise_if_not_found=False)
        if template and template.email_from != '{{ object._get_from_email() }}':
            template.write({'email_from': '{{ object._get_from_email() }}'})
