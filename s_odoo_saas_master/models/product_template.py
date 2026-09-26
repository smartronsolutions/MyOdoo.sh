from odoo import models, fields, api


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    can_be_user_app = fields.Boolean(string='Can be User and App')
    is_saas_user = fields.Boolean(string='Is SaaS User', help="Technical field use to invisible in pricing page")
    technical_name = fields.Char('Technical Name')
    ecom_category_id = fields.Many2one('product.public.category', 'App. Category')
    required_product_ids = fields.Many2many('product.product', string='Requires product', compute='_compute_required_product_ids', inverse='_inverse_required_product_ids')
    dependent_product_ids = fields.Many2many('product.product', string='Dependent product', compute='_compute_dependent_product_ids', inverse='_inverse_dependent_product_ids')

    _sql_constraints = [
        ('technical_name_uniq', 'unique (technical_name)', "Technical Name already exists !"),
    ]

    @api.depends('product_variant_ids', 'product_variant_ids.required_product_ids')
    def _compute_required_product_ids(self):
        for r in self:
            r.required_product_ids = r.product_variant_id.required_product_ids

    def _inverse_required_product_ids(self):
        for r in self:
            r.product_variant_id.required_product_ids = r.required_product_ids

    @api.depends('product_variant_ids', 'product_variant_ids.dependent_product_ids')
    def _compute_dependent_product_ids(self):
        for r in self:
            r.dependent_product_ids = r.product_variant_id.dependent_product_ids

    def _inverse_dependent_product_ids(self):
        for r in self:
            r.product_variant_id.dependent_product_ids = r.dependent_product_ids
