/** @odoo-module **/

import { Component } from "@odoo/owl";

export class SaaSBlockUI extends Component {
    static props = {
        message: { type: String, optional: true },
    };
    static template = "s_odoo_saas_master.SaaSBlockUI";
}
