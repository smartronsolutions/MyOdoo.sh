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

  /* FAQ accordion (home and about pages). Delegated, and shared by every page
     so there is a single source of truth for the open/close behaviour. */
  document.addEventListener('click', function (event) {
    var target = event.target;
    if (!target || !target.closest) return;
    var question = target.closest('.faq-q');
    if (!question) return;
    var item = question.closest('.faq-item');
    if (!item) return;
    event.preventDefault();
    var wasOpen = item.classList.contains('open');
    var container = item.parentElement;
    if (container) {
      container.querySelectorAll('.faq-item.open').forEach(function (other) {
        other.classList.remove('open');
        var otherQuestion = other.querySelector('.faq-q');
        if (otherQuestion) otherQuestion.setAttribute('aria-expanded', 'false');
      });
    }
    item.classList.toggle('open', !wasOpen);
    question.setAttribute('aria-expanded', wasOpen ? 'false' : 'true');
  });

  /* Page reveal-on-scroll entrance (home + services). The hidden state is only
     applied when JavaScript is available, so content stays visible without it. */
  var revealRoots = document.querySelectorAll('.mh');
  if (revealRoots.length && 'IntersectionObserver' in window) {
    document.documentElement.classList.add('mh-anim');
    var revealObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add('is-visible');
          revealObserver.unobserve(entry.target);
        }
      });
    }, { rootMargin: '0px 0px -6% 0px', threshold: 0.06 });
    revealRoots.forEach(function (root) {
      root.querySelectorAll('[data-reveal]').forEach(function (el) {
        revealObserver.observe(el);
      });
    });
  }

  /* Animate dashboard sparklines once they enter the viewport. */
  var sparkLines = document.querySelectorAll('.mh .mh-spark-line');
  if (sparkLines.length && 'IntersectionObserver' in window) {
    var sparkObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          var line = entry.target;
          var length = line.getTotalLength ? line.getTotalLength() : 0;
          if (length) {
            line.style.strokeDasharray = length;
            line.style.strokeDashoffset = length;
            line.style.transition = 'stroke-dashoffset 1.6s ease-out';
            requestAnimationFrame(function () {
              requestAnimationFrame(function () { line.style.strokeDashoffset = 0; });
            });
          }
          sparkObserver.unobserve(line);
        }
      });
    }, { threshold: 0.3 });
    sparkLines.forEach(function (line) { sparkObserver.observe(line); });
  }

  /* ------------------------------------------------------------------
   * SaaS enquiry form (Contact Us page)
   *   - inline validation under each field
   *   - disables the button and switches to "Sending request..." on submit
   *   - blocks duplicate submits from the same page view
   * ------------------------------------------------------------------ */
  var enquiryForm = document.getElementById('saasEnquiryForm');
  if (enquiryForm) {
    var submitBtn = enquiryForm.querySelector('.cf-submit');
    var originalLabel = submitBtn ? submitBtn.textContent : '';
    var submitting = false;

    var fieldWrap = function (input) {
      return input ? input.closest('.cf-field') : null;
    };

    var setError = function (input, hasError) {
      var wrap = fieldWrap(input);
      if (wrap) wrap.classList.toggle('has-error', !!hasError);
    };

    var isValidEmail = function (value) {
      return /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(value);
    };

    // Live feedback: clear the error as soon as the field becomes valid.
    enquiryForm.querySelectorAll('input, select, textarea').forEach(function (input) {
      input.addEventListener('input', function () {
        if (input.required && input.value.trim() !== '') setError(input, false);
        if (input.type === 'email' && isValidEmail(input.value.trim())) setError(input, false);
      });
      input.addEventListener('change', function () {
        if (input.required && input.value.trim() !== '') setError(input, false);
      });
    });

    enquiryForm.addEventListener('submit', function (event) {
      if (submitting) {
        event.preventDefault();
        return;
      }

      var firstInvalid = null;
      enquiryForm.querySelectorAll('[required]').forEach(function (input) {
        var value = (input.value || '').trim();
        var invalid = !value;
        if (!invalid && input.type === 'email') invalid = !isValidEmail(value);
        setError(input, invalid);
        if (invalid && !firstInvalid) firstInvalid = input;
      });

      if (firstInvalid) {
        event.preventDefault();
        firstInvalid.focus();
        firstInvalid.scrollIntoView({ block: 'center', behavior: 'smooth' });
        return;
      }

      submitting = true;
      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.textContent = 'Sending request...';
      }
      // Safety net: if the request is somehow cancelled, restore the button.
      setTimeout(function () {
        if (submitting && submitBtn && document.body.contains(submitBtn)) {
          submitting = false;
          submitBtn.disabled = false;
          submitBtn.textContent = originalLabel;
        }
      }, 12000);
    });
  }

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
