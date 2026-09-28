import re

from odoo import fields, models


class PSQLVersion(models.Model):
    _name = 'saas.psql.version'
    _description = "SaaS PSQL Version"

    name = fields.Char(string="Odoo Version", required=True)
    docker_image_tag = fields.Char(string='Docker Image Tag', required=True)
    active = fields.Boolean(string="Active", default=True)

    # The official ``postgres:<tag>`` images do not carry pgvector, and installing it inside
    # an existing container would need apt plus a restart of the database. The
    # ``pgvector/pgvector:pg<N>`` images are built from that very same official image with
    # the extension already compiled and installed, so the entrypoint, the environment
    # variables and the PGDATA layout are identical: it is a drop-in replacement and a data
    # directory created by the plain image keeps working.
    PGVECTOR_IMAGE_REPO = 'pgvector/pgvector'

    def _get_postgres_image(self):
        """PostgreSQL image to use for this version, with pgvector available inside it.

        The pgvector tag is derived from the major number of ``docker_image_tag``, so
        ``17`` gives ``pgvector/pgvector:pg17``: every PostgreSQL version on offer gets a
        matching image without anything to configure by hand. A tag we cannot read a
        version number from falls back to the plain image instead of guessing a wrong one.
        """
        self.ensure_one()
        tag = (self.docker_image_tag or '').strip()
        match = re.match(r'^(\d+)', tag)
        if not match:
            return 'postgres:%s' % tag
        return '%s:pg%s' % (self.PGVECTOR_IMAGE_REPO, match.group(1))
