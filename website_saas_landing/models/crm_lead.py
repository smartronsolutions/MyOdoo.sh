# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import fields, models

from .website_saas_enquiry import INTERESTED_IN_SELECTION


class CrmLead(models.Model):
    """Enrich leads/opportunities coming from the website enquiry form.

    A ``crm.lead`` is created automatically for every ``website.saas.enquiry``
    (see ``WebsiteSaasEnquiry._create_crm_lead``). These fields carry the
    enquiry-specific information that has no native CRM equivalent.
    """

    _inherit = 'crm.lead'

    saas_enquiry_id = fields.Many2one(
        'website.saas.enquiry', string='SaaS Enquiry',
        copy=False, index=True, ondelete='set null',
        help="Website enquiry this lead was created from.",
    )
    saas_interested_in = fields.Selection(
        selection=INTERESTED_IN_SELECTION,
        string='Interested In',
    )
    saas_odoo_version = fields.Char(string='Current Odoo Version')
    saas_estimated_users = fields.Char(string='Estimated Users')
    saas_country = fields.Char(string='Country (Enquiry)')
    saas_source_page = fields.Char(string='Source Page')
    saas_lang = fields.Char(string='Enquiry Language')
