from odoo import api, fields, models


class ProductProduct(models.Model):
    _inherit = 'product.product'

    required_product_ids = fields.Many2many('product.product', 'require_product_id', 'product_id', 'require_product_id', string='Requires product')
    dependent_product_ids = fields.Many2many('product.product', 'require_product_id', 'require_product_id', 'product_id', string='Dependent product')

    @api.model
    def _create_saas_worker_product(self):
        """Create the paid "Workers" seat product on the fly.

        Used as a last-resort fallback when the XML data record is missing
        (e.g. a database restored without the module data). It reuses the
        legacy seat UoM / category and the ``is_saas_user`` technical flag so
        every existing seat-related query keeps working.
        """
        Product = self.sudo()
        vals = {
            'name': 'Workers',
            'default_code': 'saas_worker',
            'list_price': 100.0,
            'can_be_user_app': True,
            'type': 'service',
            'website_sequence': 1000,
            'is_published': True,
            'is_saas_user': True,
        }
        uom = self.env.ref('s_odoo_saas_master.product_uom_saas_user_month', raise_if_not_found=False)
        if uom:
            vals['uom_id'] = uom.id
            vals['uom_po_id'] = uom.id
        categ = self.env.ref('s_odoo_saas_master.public_category_saas_user', raise_if_not_found=False)
        if categ:
            vals['ecom_category_id'] = categ.id
        return Product.create(vals)

    @api.model
    def _get_saas_worker_product(self):
        """Return the active paid "Workers" seat product, creating it if missing.

        Resolution order (first hit wins):
        ``product_saas_worker`` XML id -> ``default_code`` ``saas_worker``
        -> active ``is_saas_user`` product -> active product named ``Workers``
        -> legacy ``product_saas_user`` (un-archived) -> create one.
        """
        Product = self.sudo()

        prod = self.env.ref('s_odoo_saas_master.product_saas_worker', raise_if_not_found=False)
        if prod and prod.exists() and prod.active:
            return prod.sudo()

        prod = Product.search([('default_code', '=ilike', 'saas_worker'), ('active', '=', True)], limit=1)
        if not prod:
            prod = Product.search([('is_saas_user', '=', True), ('active', '=', True)], limit=1)
        if not prod:
            prod = Product.search([('name', 'ilike', 'Worker'), ('active', '=', True)], limit=1)
        if prod:
            return prod

        # Legacy fallback: reuse the archived "SaaS User" product if the new one
        # is nowhere to be found (e.g. partially restored database).
        legacy = self.env.ref('s_odoo_saas_master.product_saas_user', raise_if_not_found=False)
        if not legacy or not legacy.exists():
            legacy = Product.with_context(active_test=False).search(
                [('default_code', '=ilike', 'saas_user')], limit=1)
        if legacy and legacy.exists():
            legacy.with_context(active_test=False).write({'active': True})
            legacy.product_tmpl_id.with_context(active_test=False).write({'active': True})
            return legacy.sudo()

        return self._create_saas_worker_product()

    def get_required_products(self):
        self.ensure_one()

        def get_required(product):
            products = product.required_product_ids
            for p in product.required_product_ids:
                products |= get_required(p)
            return products

        required_products = get_required(self)
        return required_products.ids

    def get_dependent_products(self):
        self.ensure_one()

        def get_dependent(product):
            products = product.dependent_product_ids
            for p in product.dependent_product_ids:
                products |= get_dependent(p)
            return products

        dependent_products = get_dependent(self)
        return dependent_products.ids
