/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import publicWidget from "@web/legacy/js/public/public_widget";
import { SaaSBlockUI } from "../components/saas_block_ui";
import SaaSAuth from "./saas_auth_guard";
import { session } from "@web/session";
import { rpc } from '@web/core/network/rpc';

const mainComponentRegistry = registry.category("main_components");

publicWidget.registry.SaasPortalPricing = publicWidget.Widget.extend({
	selector: 'form.openerp_enterprise_pricing_form',
    events: {
        'click .openerp_enterprise_pricing_app': '_onToggleApp',
        "click li[data-type='monthly']": '_onSwitchMonthly',
        "click li[data-type='yearly']": '_onSwitchYearly',
        "change input.openerp_enterprise_pricing_users": '_onChangeUsers',
        "click a.openerp_enterprise_pricing_trial": '_onClickTrial',
        "click a.openerp_enterprise_pricing_buy_now": '_onClickBuy',
        "click a.o_free_trial_button": '_onStartFreeTrial',
    },

    init() {
        this._super.apply(this, arguments);
		this.rpc = rpc
		this.notification = this.bindService("notification");
		this.session = session;
        this.subscriptionType = 'yearly';
        this.usersCount = 1;
        this.appsList = new Set();
        this.appsCount = 0;
        this.monthlyPricelist = {};
        this.yearlyPricelist = {};
        this.pricelistId = false;
        this.currency = {};
    },

    start() {
        this.pricelistId = parseInt(this.$('.openerp_enterprise_pricing_pricelist').data('pricelist'));
        this._getPriceList();
        this._super.apply(this, arguments);
        // Restore the configuration remembered before a guest signed in.
        SaaSAuth.restorePendingAction();
    },
	
	formatNumber(num) {
        return parseFloat((num).toFixed(this.currency.decimal_places)).toLocaleString();
    },
    
    formatPrice(num) {
        const formattedNumber = this.formatNumber(num);
        if (this.currency.position === 'before') {
            return this.currency.symbol + ' ' + formattedNumber;
        } else {
            return formattedNumber + ' ' + this.currency.symbol;
        }
    },
	
	formatPrice(num) {
        const formattedNumber = this.formatNumber(num);
        if (this.currency.position === 'before') {
            return this.currency.symbol + ' ' + formattedNumber;
        } else {
            return formattedNumber + ' ' + this.currency.symbol;
        }
    },
	
	async _getPriceList() {		
		var pricelist = await this.rpc('/pricing/get-saas-pricelist', {pricelist_id: this.pricelistId});
		this.monthlyPricelist = pricelist.monthly_pricelist;
        this.yearlyPricelist = pricelist.yearly_pricelist;
        this.currency = pricelist.currency;
        this._recomputePriceBoard();     
    },
	
	_recomputePriceBoard() {
        var priceData = this._computePriceData();
        this.$('.openerp_enterprise_pricing_users_num').text(this.usersCount);
        this.$('.openerp_enterprise_pricing_apps_num').text(this.appsCount);
        
        // Update the user input price display
        var userPrice = this._getUserPrice();
        if (this.subscriptionType === 'monthly') {
            this.$('.openerp_enterprise_pricing_user_amount_monthly').text(this.formatPrice(userPrice));
        } else {
            this.$('.openerp_enterprise_pricing_user_amount_yearly').text(this.formatPrice(userPrice));
        }
        
        if (this.subscriptionType === 'monthly') {
            this.$('.openerp_enterprise_pricing_users_price_monthly').text(this.formatPrice(priceData.usersAmount));
            this.$('.openerp_enterprise_pricing_apps_price_monthly').text(this.formatPrice(priceData.appsAmount));
            this.$('.openerp_enterprise_pricing_price_monthly').text(this.formatPrice(priceData.usersAmount + priceData.appsAmount));
        } else {
            this.$('.openerp_enterprise_pricing_users_price_yearly').text(this.formatPrice(priceData.usersAmount));
            this.$('.openerp_enterprise_pricing_apps_price_yearly').text(this.formatPrice(priceData.appsAmount));
            this.$('.openerp_enterprise_pricing_price_yearly').text(this.formatPrice(priceData.usersAmount + priceData.appsAmount));
            // Workers are NOT multiplied by 12 (one price, monthly or annual); only the
            // apps are really billed for the twelve months.
            this.$('.openerp_enterprise_pricing_price_yearly_in_year').text(
                this.formatPrice(priceData.usersAmount + priceData.appsAmount * 12));
        }
    },
	
	_computePriceData() {
        let userPrice = this._getUserPrice();
        var usersAmount = this.usersCount * userPrice;
        var appsAmount = 0;
        var self = this;
        this.appsList.forEach(function (appId, value2, set) {
            appsAmount = appsAmount + self._getAppPrice(appId);
        });
        return {
            usersAmount: usersAmount,
            appsAmount: appsAmount,
        }
    },
	
	_getUserPrice() {
        var userId = parseInt(this.$('input.openerp_enterprise_pricing_users').data('app_id'));
        // Workers have ONE price: the same amount on monthly and on annual billing, so the
        // monthly pricelist is used whatever the selected cycle (never x12, never /12).
        return this.monthlyPricelist[userId] || 0;
    },
	
	_getAppPrice(appId) {
        var price;
        if (this.subscriptionType === 'monthly') {
            price = this.monthlyPricelist[appId] || 0;
        } else {
            price = this.yearlyPricelist[appId] / 12 || 0;
        }
        return price;
    },
	
	async _onToggleApp(ev) {
        ev.preventDefault();
        ev.stopPropagation();
        var $appBtn = $(ev.currentTarget);
        var appId = $appBtn.data('app_id');
        var checked = $appBtn.find('input.openerp_enterprise_pricing_app_checkbox').is(":checked");
        this._toggleApp(appId, !checked);
        await this._toggleRelatingApps(appId, !checked);
        this.appsCount = this.appsList.size;
        this._recomputePriceBoard();
    },
	
	async _toggleRelatingApps(appId, state) {
        var self = this;
        if (state) {
			var requiredAppIds = await this.rpc('/pricing/get-required-apps', {app_id: appId});
			$.each(requiredAppIds, function (index, requiredAppId) {
                self._toggleApp(requiredAppId, true);
            });
        } else {
			var dependAppIds = await this.rpc('/pricing/get-dependent-apps', {app_id: appId});
			$.each(dependAppIds, function (index, dependAppId) {
                self._toggleApp(dependAppId, false);
            });
        }
    },
	
	_toggleApp(appId, state) {
        var $appBtn = this.$el.find(".openerp_enterprise_pricing_app[data-app_id='" + appId.toString() + "']");
        var $input = $appBtn.find('input.openerp_enterprise_pricing_app_checkbox');
        if (state) {
            $appBtn.addClass('checked');
            $appBtn.find('span.fa-check').removeClass('d-none');
            $input.prop('checked', true);
            this.appsList.add(appId);
        } else {
            $appBtn.removeClass('checked');
            $appBtn.find('span.fa-check').addClass('d-none');
            $input.prop('checked', false);
            this.appsList.delete(appId);
        }
    },
	
	_onSwitchMonthly(ev) {
        this.subscriptionType = 'monthly';
        this.$('input[name=price_by]').val("monthly");
        this.$('.openerp_enterprise_user_pricing_monthly').removeClass('d-none');
        this.$('.openerp_enterprise_user_pricing_yearly').addClass('d-none');
        this.$('.openerp_enterprise_pricing_app_price.hide_monthly_apps').removeClass('d-none');
        this.$('.openerp_enterprise_pricing_app_price.hide_yearly_apps').addClass('d-none');
        this._recomputePriceBoard();
    },
	
	_onSwitchYearly(ev) {
        this.subscriptionType = 'yearly';
        this.$('input[name=price_by]').val("yearly");
        this.$('.openerp_enterprise_user_pricing_monthly').addClass('d-none');
        this.$('.openerp_enterprise_user_pricing_yearly').removeClass('d-none');
        this.$('.openerp_enterprise_pricing_app_price.hide_monthly_apps').addClass('d-none');
        this.$('.openerp_enterprise_pricing_app_price.hide_yearly_apps').removeClass('d-none');
        this._recomputePriceBoard();
    },
	
	_onChangeUsers(ev) {
        var $input = $(ev.currentTarget);
        this.usersCount = parseInt($input.val());
        this._recomputePriceBoard();
    },
	
	async _checkDomain() {
        this.$('.odoo_domain_picking_error').empty();
        this.$('input#sub_domain').removeClass('has-error');
        var subDomain = this.$('input.openerp_enterprise_pricing_sub_domain').val();
        if (/^\d/.test(subDomain)){
			this.notification.add(_t("Your subdomain cannot start with a number."), { type: 'warning', sticky: true });
			this.$('input#sub_domain').addClass('has-error');
			return false;
		}
        if (!subDomain) {
			this.notification.add(_t("You have to choose a domain for your instance."), { type: 'warning', sticky: true });
            this.$('input#sub_domain').addClass('has-error');
            return false;
        } else if (!/^[a-z0-9\-]+$/g.test(subDomain)) {
            this.notification.add(_t("Your domain can only contains characters from 'a' to 'z', '0' to '9' and '-'."), { type: 'warning', sticky: true });
            this.$('input#sub_domain').addClass('has-error');
            return false;
        }
        var domainId = parseInt(this.$('select.openerp_enterprise_pricing_domain').val());
        this.$('.odoo_domain_picking_error').append($('<i class="fa fa-spinner fa-spin fa-fw"></i>'));
        var result = await this.rpc('/pricing/check-domain', {sub_domain: subDomain, domain_id: domainId});
        this.$('.odoo_domain_picking i.fa-spinner').remove();
        if (result.error) {
            this.notification.add(result.error, { type: 'warning', sticky: true });
            return false;
        }
        return true;
    },
	
	async _onClickTrial(ev) {
        ev.preventDefault();
        ev.stopPropagation();

        // Guest visitors must sign in / create an account before an instance
        // can be created. Show the dedicated popup instead of a technical
        // error coming back from the deployment RPC.
        if (!SaaSAuth.isAuthenticated()) {
            SaaSAuth.openAccountRequiredModal({
                action: "trial",
                targetId: (ev.currentTarget && ev.currentTarget.id) || "",
                form: this.el,
            });
            return;
        }

        // Check domain first before doing any authentication checks
        var checkResult = await this._checkDomain();
        if (!checkResult) {
            return;
        }
        
        // Try to verify login status by calling check_trial endpoint
        try {
            var checkTrial = await this.rpc('/pricing/check-trial', {});
            if (checkTrial === false) {
                this.notification.add(_t('You have reached the maximum number of trials. Please use the paid Odoo instance.'), { type: 'warning', sticky: true });
                return false;
            }
        } catch (e) {
            // If RPC fails with authentication error, user is not logged in
            this.notification.add(_t('Please login to start your trial'), { type: 'warning', sticky: true });
            return false;
        }
        
        var $trial = this.$('input.openerp_enterprise_pricing_app_trial');
        $trial.prop('checked', true);        
        this.blockUI(_t("Your Odoo instance is being deployed. Please wait a few minutes."));
        
        try {
            const instance_vals = await this._prepare_instance_vals();
            var instance = await this.rpc('/saas/instance/create-trial', { instance_vals });
            this.unblockUI();
            if (instance && instance.error) {
                this.notification.add(instance.error, { type: 'danger', sticky: true });
                return false;
            }
            if (instance && instance.id) {
                window.location.href = "/my/saas/odoo-instance/" + instance.id;
            } else {
                this.notification.add(_t('An error occurred while creating your trial instance. Please try again.'), { type: 'danger', sticky: true });
            }
        } catch (e) {
            this.unblockUI();
            console.error(e.message);
            // Session expired / not authenticated: ask for an account rather
            // than showing a technical RPC error.
            if (!SaaSAuth.isAuthenticated() || SaaSAuth.isAuthenticationError(e)) {
                SaaSAuth.openAccountRequiredModal({ action: "trial", form: this.el });
                return false;
            }
            const errMsg = e.data?.message || e.message || _t('An error occurred while creating your trial instance. Please try again.');
            this.notification.add(errMsg, { type: 'danger', sticky: true });
        }
    },
	
	async _prepare_instance_vals() {
		const subDomain = this.$('input.openerp_enterprise_pricing_sub_domain').val();
		const domainId = parseInt(this.$('select.openerp_enterprise_pricing_domain').val());
		const subscriptionType = this.$('input#yearly_by').val();
		const addIds = [];
		$("input.openerp_enterprise_pricing_app_checkbox:checkbox:checked").each(function() {
			 addIds.push($(this).attr("id"))			 
		});
		
		const instance_vals = {
			sub_domain: subDomain,
			base_domain_id: domainId,
			subscription_type: subscriptionType,
			default_app_ids: addIds
		}
		
		return instance_vals
	},

    async _onClickBuy(ev) {
        ev.preventDefault();
        ev.stopPropagation();

        // Ordering / deploying requires an account too.
        if (!SaaSAuth.isAuthenticated()) {
            SaaSAuth.openAccountRequiredModal({
                action: "order",
                targetId: (ev.currentTarget && ev.currentTarget.id) || "",
                form: this.el,
            });
            return false;
        }

        var checkResult = await this._checkDomain();
        if (!checkResult) {
            return;
        }
        await this.$el.submit();
    },

    _onStartFreeTrial(ev) {
        return this._onClickTrial(ev);
    },
	
	blockUI(message) {
        mainComponentRegistry.add(
            "SaaSBlockUI",
            {
                Component: SaaSBlockUI,
                props: {
                    message,
                },
            },
            { force: true }
        );
    },

    unblockUI() {
        mainComponentRegistry.remove("SaaSBlockUI");
    }

});
