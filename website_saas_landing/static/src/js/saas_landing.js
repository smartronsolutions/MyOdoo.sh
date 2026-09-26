/**
 * Website SaaS Landing Page - External JavaScript
 * This file can be used for additional functionality or enhancements
 * Main scripts are embedded in the template for better performance
 */

(function() {
  'use strict';

  /**
   * Initialize landing page enhancements
   */
  function initLandingPage() {
    document.documentElement.classList.add('mo_saas_ready');
  }

  function closeAccountDropdown() {
    var account = document.getElementById('mo_account');
    if (!account) return;
    var button = account.querySelector('#mo_account_btn');
    var dropdown = account.querySelector('#mo_account_drop');
    if (button) {
      button.classList.remove('mo_active_btn');
      button.setAttribute('aria-expanded', 'false');
    }
    if (dropdown) dropdown.classList.remove('mo_open');
  }

  /* Language dropdown (English / Français / Español). The actual language
     switch is handled by Odoo's native `.js_change_lang` handler; here we only
     open/close the menu so it works on the website header AND the /my topbar. */
  function closeLangDropdown() {
    var wrap = document.getElementById('mo_lang');
    if (!wrap) return;
    var button = wrap.querySelector('#mo_lang_btn');
    var dropdown = wrap.querySelector('#mo_lang_drop');
    if (button) {
      button.classList.remove('mo_active_btn');
      button.setAttribute('aria-expanded', 'false');
    }
    if (dropdown) dropdown.classList.remove('mo_open');
  }

  /* Delegation survives Odoo frontend navigation and dynamic layout updates. */
  document.addEventListener('click', function (event) {
    var target = event.target;
    if (!target || !target.closest) return;

    var langButton = target.closest('#mo_lang_btn');
    if (langButton) {
      event.preventDefault();
      event.stopPropagation();
      var langWrap = langButton.closest('#mo_lang');
      var langDropdown = langWrap && langWrap.querySelector('#mo_lang_drop');
      if (!langDropdown) return;
      var langOpening = !langDropdown.classList.contains('mo_open');
      closeLangDropdown();
      if (langOpening) {
        langDropdown.classList.add('mo_open');
        langButton.classList.add('mo_active_btn');
        langButton.setAttribute('aria-expanded', 'true');
      }
      return;
    }

    var accountButton = target.closest('#mo_account_btn');
    if (accountButton) {
      event.preventDefault();
      event.stopPropagation();
      var account = accountButton.closest('#mo_account');
      var dropdown = account && account.querySelector('#mo_account_drop');
      if (!dropdown) return;
      var opening = !dropdown.classList.contains('mo_open');
      closeAccountDropdown();
      if (opening) {
        dropdown.classList.add('mo_open');
        accountButton.classList.add('mo_active_btn');
        accountButton.setAttribute('aria-expanded', 'true');
      }
      return;
    }

    if (!target.closest('#mo_account')) closeAccountDropdown();
    if (!target.closest('#mo_lang')) closeLangDropdown();
  });

  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') {
      closeAccountDropdown();
      closeLangDropdown();
    }
  });

  /**
   * Handle window load event
   */
  window.addEventListener('load', function() {
    initLandingPage();
  });

  /**
   * Handle DOM ready
   */
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initLandingPage);
  } else {
    initLandingPage();
  }

})();
