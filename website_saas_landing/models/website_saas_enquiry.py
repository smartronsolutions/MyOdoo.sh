# Part of Odoo. See LICENSE file for full copyright and licensing details.

import logging
import re

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)

ENQUIRY_SEQUENCE_CODE = 'website.saas.enquiry'
NOTIFICATION_EMAIL_PARAM = 'website_saas_landing.enquiry_recipient_email'

EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


class WebsiteSaasEnquiry(models.Model):
    """Enquiry submitted from the public website (Contact Us / SaaS pages).

    Records are always created by the public controller; only internal users
    have access to the backend list, kanban and form views.
    """

    _name = 'website.saas.enquiry'
    _description = 'Website SaaS Enquiry'
    _order = 'submission_date desc, id desc'
    _rec_name = 'name'

    name = fields.Char(
        string='Enquiry Number', required=True, copy=False, readonly=True,
        default=lambda self: _('New'),
    )
    submission_date = fields.Datetime(
        string='Submitted On', default=fields.Datetime.now, readonly=True, copy=False,
    )

    # Contact details
    full_name = fields.Char(string='Full Name', required=True)
    company_name = fields.Char(string='Company Name', required=True)
    email = fields.Char(string='Email', required=True)
    phone = fields.Char(string='Phone / WhatsApp')
    country = fields.Char(string='Country')

    # Requirement
    interested_in = fields.Selection(
        selection=[
            ('implementation', 'New Odoo Implementation'),
            ('hosting', 'Odoo Hosting / MyOdoo.sh'),
            ('migration', 'Odoo Migration'),
            ('customisation', 'Odoo Customisation'),
            ('module_development', 'Custom Module Development'),
            ('integration', 'Integration / API'),
            ('backup_recovery', 'Backup & Recovery'),
            ('support_maintenance', 'Support & Maintenance'),
            ('existing_project', 'Existing Odoo Project'),
            ('other', 'Other'),
        ],
        string='Interested In', required=True, default='implementation',
    )
    odoo_version = fields.Char(string='Current Odoo Version')
    estimated_users = fields.Char(string='Estimated Users')
    message = fields.Text(string='Project Details', required=True)

    # Origin
    source_page = fields.Char(string='Source Page', readonly=True)
    lang = fields.Char(string='Language', readonly=True)
    company_id = fields.Many2one(
        'res.company', string='Company', index=True,
        default=lambda self: self.env.company,
    )

    # Follow-up
    state = fields.Selection(
        selection=[
            ('new', 'New'),
            ('contacted', 'Contacted'),
            ('qualified', 'Qualified'),
            ('in_progress', 'In Progress'),
            ('won', 'Won'),
            ('closed', 'Closed'),
        ],
        string='Status', default='new', required=True, group_expand='_group_expand_states',
    )
    user_id = fields.Many2one('res.users', string='Assigned To', index=True)
    internal_notes = fields.Text(string='Internal Notes')

    # ------------------------------------------------------------------
    # ORM
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    ENQUIRY_SEQUENCE_CODE
                ) or _('New')
        return super().create(vals_list)

    @api.model
    def _group_expand_states(self, states, domain, order=None):
        return [key for key, _label in self._fields['state'].selection]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @api.model
    def _get_notification_email(self):
        """Return the address that receives new-enquiry notifications.

        Configured from Settings; falls back to the company address so nothing
        is hard-coded. Returns an empty string when nothing is configured.
        """
        self_sudo = self.sudo()
        recipient = self_sudo.env['ir.config_parameter'].sudo().get_param(
            NOTIFICATION_EMAIL_PARAM, ''
        )
        if recipient and EMAIL_RE.match(recipient.strip()):
            return recipient.strip()
        company = self_sudo.env.company
        return company.email or company.partner_id.email or ''

    @api.model
    def normalise_email(self, email):
        """Return a normalised email address or an empty string when invalid."""
        email = (email or '').strip()
        return email if EMAIL_RE.match(email) else ''

    def _notify_new_enquiry(self):
        """Send the internal notification and the customer confirmation."""
        self.ensure_one()
        # Internal notification
        recipient = self._get_notification_email()
        if recipient:
            template = self.env.ref(
                'website_saas_landing.mail_template_enquiry_internal',
                raise_if_not_found=False,
            )
            if template:
                try:
                    template.send_mail(
                        self.id,
                        email_values={'email_to': recipient},
                        force_send=False,
                    )
                except Exception:  # noqa: BLE001 - never break the visitor flow
                    _logger.exception(
                        "SaaS enquiry %s: internal notification could not be queued.",
                        self.name,
                    )
        else:
            _logger.warning(
                "SaaS enquiry %s: no notification address configured "
                "(Settings > Website > SaaS Enquiry Recipient Email).",
                self.name,
            )

        # Customer confirmation
        confirmation = self.env.ref(
            'website_saas_landing.mail_template_enquiry_customer',
            raise_if_not_found=False,
        )
        if confirmation and self.email:
            try:
                confirmation.send_mail(self.id, force_send=False)
            except Exception:  # noqa: BLE001
                _logger.exception(
                    "SaaS enquiry %s: customer confirmation could not be queued.",
                    self.name,
                )
