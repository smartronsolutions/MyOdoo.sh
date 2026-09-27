# Part of Odoo. See LICENSE file for full copyright and licensing details.

import logging

from odoo import http, _
from odoo.http import request
from odoo.tools import html_escape

_logger = logging.getLogger(__name__)

# Duplicate guard: identical submissions inside this window are ignored.
DUPLICATE_WINDOW_MINUTES = 3


class WebsiteSaasEnquiryController(http.Controller):
    """Handle public submissions of the SaaS enquiry form."""

    @http.route(
        '/website_saas_landing/enquiry',
        type='http',
        auth='public',
        methods=['POST'],
        csrf=True,
        website=True,
        save_session=True,
    )
    def submit_enquiry(self, **post):
        Enquiry = request.env['website.saas.enquiry'].sudo()

        values = {
            'full_name': (post.get('full_name') or '').strip(),
            'company_name': (post.get('company_name') or '').strip(),
            'email': Enquiry.normalise_email(post.get('email')),
            'phone': (post.get('phone') or '').strip(),
            'country': (post.get('country') or '').strip(),
            'interested_in': (post.get('interested_in') or '').strip(),
            'odoo_version': (post.get('odoo_version') or '').strip(),
            'estimated_users': (post.get('estimated_users') or '').strip(),
            'message': (post.get('message') or '').strip(),
            'source_page': (post.get('source_page') or request.httprequest.path)[:255],
            'lang': request.env.lang or '',
        }

        errors = self._validate(values)
        if errors:
            return self._redirect(request.params.get('redirect'), error=','.join(errors))

        if not values['interested_in']:
            values['interested_in'] = 'other'

        # --- Duplicate protection ------------------------------------------------
        # 1. Browser refresh / double submit of the exact same payload.
        duplicate = Enquiry.search([
            ('email', '=', values['email']),
            ('message', '=', values['message']),
            ('create_date', '>=', self._duplicate_limit()),
        ], limit=1)

        # 2. Same session submitting the same requirement again.
        session_key = 'saas_enquiry_last'
        payload_key = '%s|%s' % (values['email'], values['message'][:80])
        if not duplicate and request.session.get(session_key) == payload_key:
            duplicate = Enquiry.search([
                ('email', '=', values['email']),
            ], limit=1, order='id desc')

        enquiry = duplicate
        if not enquiry:
            try:
                enquiry = Enquiry.create(values)
                request.session[session_key] = payload_key
            except Exception:  # noqa: BLE001
                _logger.exception("SaaS enquiry: record could not be created.")
                return self._redirect(
                    request.params.get('redirect'), error='server_error'
                )
            enquiry._notify_new_enquiry()

        # POST/redirect/GET so a browser refresh never resubmits the form.
        return self._redirect(request.params.get('redirect'), enquiry=enquiry.name)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _validate(values):
        """Return a list of invalid field names."""
        errors = []
        if not values['full_name']:
            errors.append('full_name')
        if not values['company_name']:
            errors.append('company_name')
        if not values['email']:
            errors.append('email')
        if not values['message']:
            errors.append('message')
        if values['interested_in'] and values['interested_in'] not in dict(
            request.env['website.saas.enquiry']._fields['interested_in'].selection
        ):
            values['interested_in'] = 'other'
        return errors

    @staticmethod
    def _duplicate_limit():
        from odoo import fields as odoo_fields
        return odoo_fields.Datetime.subtract(
            odoo_fields.Datetime.now(), minutes=DUPLICATE_WINDOW_MINUTES
        )

    @staticmethod
    def _redirect(redirect_to, enquiry=None, error=None):
        url = '/contact-us'
        if redirect_to and redirect_to.startswith('/') and not redirect_to.startswith('//'):
            url = redirect_to.split('?')[0] or url
        params = []
        if enquiry:
            params.append('enquiry=%s' % html_escape(enquiry))
        if error:
            params.append('enquiry_error=%s' % html_escape(error))
        if params:
            url = '%s?%s' % (url, '&'.join(params))
        return request.redirect(url + '#enquiry-form')
