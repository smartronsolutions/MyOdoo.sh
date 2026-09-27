# Website SaaS Landing Page Module

import logging

from . import models
from . import controllers

_logger = logging.getLogger(__name__)

LANDING_PAGE_XMLID = 'website_saas_landing.saas_landing_page'


def post_init_hook(env):
    """Replace the website home page with the SaaS landing page on installation.

    Odoo serves the root URL ``/`` by rerouting (internal, no 3xx) to
    ``website.homepage_url`` when set. Pointing it to the module's ``/saas``
    page makes ``/`` display the SaaS landing page while keeping the URL.

    The ``/saas`` page is not bound to a specific website, so it is available
    on every website; the home page is therefore switched on all websites that
    still use their default home page (empty or ``/``).
    """
    page = env.ref(LANDING_PAGE_XMLID, raise_if_not_found=False)
    if not page:
        _logger.warning("SaaS landing page not found; website home page not replaced.")
        return

    for website in env['website'].search([]):
        if not website.homepage_url or website.homepage_url == '/':
            website.homepage_url = page.url
            _logger.info("Website '%s': home page set to %s", website.name, page.url)


def uninstall_hook(env):
    """Restore the default root home page when the module is uninstalled.

    Only resets websites that still point at the module's landing page, so a
    home page chosen manually afterwards is preserved.
    """
    landing_page = env.ref(LANDING_PAGE_XMLID, raise_if_not_found=False)
    landing_url = landing_page.url if landing_page else '/saas'
    for website in env['website'].search([]):
        if website.homepage_url == landing_url:
            website.homepage_url = False
            _logger.info("Website '%s': home page restored to /", website.name)
