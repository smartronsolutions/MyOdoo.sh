import logging
from urllib.parse import urlparse

from odoo import fields, http
from odoo.http import request

_logger = logging.getLogger(__name__)


class SaasConnect(http.Controller):
    """One-time, passwordless hand-over between the SaaS master and a container.

    ``saas.odoo.instance.action_connect_odoo`` creates an authenticated session on
    the container itself and stores its identifier plus a random one-time token.
    The browser is then sent to ``https://<instance>/_saas_connect?token=...``: the
    instance nginx proxies that exact location back to this master (so the response
    shares the *instance* origin), and this route installs the container's session
    cookie before redirecting to its backend.

    The session cookie cannot be set from the master domain: browsers only accept
    a cookie for the exact host (or a parent domain), and a ``.myodoo.sh`` cookie
    would collide with the master's own session.
    """

    @http.route(
        '/_saas_connect', type='http', auth='public', csrf=False,
        save_session=False, sitemap=False, methods=['GET'],
    )
    def saas_connect(self, token=None, **kwargs):
        Instance = request.env['saas.odoo.instance'].sudo()
        instance = Instance
        if token:
            instance = Instance.search([('connect_token', '=', token)], limit=1)
        if not instance or not instance.connect_session_sid:
            return request.not_found()

        if instance.connect_token_expiry and instance.connect_token_expiry < fields.Datetime.now():
            instance.write({'connect_token': False, 'connect_session_sid': False})
            return request.not_found()

        # Only accept the request on the instance's own domain. The instance nginx
        # forwards the browser host in ``X-Saas-Host`` and rewrites ``Host`` to the
        # master domain; a request made directly on the master domain must never
        # install a container session cookie (it would replace the visitor's own
        # master session). Without ``X-Saas-Host`` we fall back to ``Host``.
        expected_host = (urlparse(instance.url or '').netloc or '').lower()
        seen_host = (
            request.httprequest.headers.get('X-Saas-Host')
            or request.httprequest.host
            or ''
        ).lower()
        if expected_host and seen_host != expected_host:
            _logger.warning(
                "Odoo connect: refused token for %s on host %s (expected %s).",
                instance.display_name, seen_host or '?', expected_host)
            return request.not_found()

        sid = instance.connect_session_sid
        # The token is single use: consume it before handing the session over.
        instance.write({
            'connect_token': False,
            'connect_token_expiry': False,
            'connect_session_sid': False,
        })

        response = request.redirect('/web', 303)
        response.set_cookie(
            'session_id', sid,
            max_age=7 * 24 * 3600,
            path='/',
            secure=request.httprequest.is_secure or (instance.url or '').startswith('https://'),
            httponly=True,
            samesite='Lax',
        )
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        _logger.info(
            "Odoo connect: handed the admin session of %s to uid %s.",
            instance.display_name, request.env.uid)
        return response
