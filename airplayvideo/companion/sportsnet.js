'use strict';

// Runs in the extension's isolated world. Only the fixed result below crosses
// native messaging: never return page text, URLs, account names or input values.
function sportsnetControl(command) {
  const empty = stage => ({stage, filled: false, target_matches: false,
    video_time: null, paused: true, muted: true, fullscreen: false, next: 'none',
    ...(command.action === 'sportsnet_step' ? {acted: false} : {})});
  const origins = new Set(['https://watch.sportsnet.ca', 'https://account.sportsnet.ca',
    'https://rogers-prd.transmit.security']);
  if (window.top !== window || !origins.has(location.origin)) return empty('other');
  const visible = element => {
    if (!element || !element.isConnected) return false;
    const rect = element.getBoundingClientRect();
    if (!Number.isFinite(rect.width) || !Number.isFinite(rect.height) || rect.width <= 0 || rect.height <= 0) return false;
    for (let node = element; node; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (style.display === 'none' || style.visibility !== 'visible' || Number(style.opacity) === 0) return false;
    }
    return true;
  };
  const select = selector => [...document.querySelectorAll(selector)].filter(visible);
  const label = element => (element.getAttribute('aria-label') || element.innerText || element.textContent || '')
    .replace(/\s+/g, ' ').trim().toLowerCase();
  const enabled = element => element && !element.disabled && element.getAttribute('aria-disabled') !== 'true';
  const controls = () => select('button, a[href], [role="button"], input[type="submit"]');
  const exact = names => controls().filter(element => names.includes(label(element)));
  const unique = items => items.length === 1 ? items[0] : null;
  const safeForm = (field, control) => {
    const paths = [field.form?.getAttribute('action'), control.getAttribute('formaction')].filter(Boolean);
    return paths.every(value => {
      try {
        const url = new URL(value, location.href);
        return origins.has(url.origin) && !url.username && !url.password;
      } catch (_) { return false; }
    });
  };
  const fieldRect = field => {
    if (!visible(field) || !enabled(field)) return null;
    const box = field.getBoundingClientRect();
    const width = Math.floor(innerWidth), height = Math.floor(innerHeight);
    if (![box.left, box.top, box.width, box.height, width, height].every(Number.isFinite) ||
        width < 1 || height < 1 || width > 16384 || height > 16384 ||
        box.left < 0 || box.top < 0 || box.right > width || box.bottom > height) return null;
    const x = Math.floor(box.left), y = Math.floor(box.top);
    const w = Math.floor(box.width), h = Math.floor(box.height);
    if (w < 1 || h < 1) return null;
    const top = document.elementFromPoint(x + Math.floor(w / 2), y + Math.floor(h / 2));
    if (!top || !(field === top || field.contains(top))) return null;
    return {x, y, width: w, height: h, viewport_width: width, viewport_height: height};
  };
  const allowedTarget = value => {
    if (typeof value !== 'string' || value.length > 2048) return null;
    try {
      const url = new URL(value);
      if (url.origin !== 'https://watch.sportsnet.ca' || url.username || url.password || url.search || url.hash ||
          !/^\/(?:$|(?:watch|event|channel)\/[A-Za-z0-9_-]+\/?$)/.test(url.pathname)) return null;
      return url;
    } catch (_) { return null; }
  };
  const result = empty('unknown');
  const target = allowedTarget(command.target_url);
  result.target_matches = !!target && location.origin === target.origin &&
    location.pathname.replace(/\/$/, '') === target.pathname.replace(/\/$/, '');

  // Challenge checks precede credential handling. A passive reCAPTCHA badge is
  // present during normal login; it is not an active verification challenge.
  const challenge = select('input[autocomplete="one-time-code"], input[name="otp"], input[name="verificationCode"], input[name="verification_code"], input[autocomplete="new-password"], iframe[src*="/recaptcha/api2/bframe"], iframe[src*="/recaptcha/enterprise/bframe"], iframe[src*="hcaptcha.com"][title*="challenge"]');
  const challengeLabels = select('[role="dialog"], h1, h2, h3, [role="heading"], label').some(element =>
    /^(?:verify (?:your |it.s )?(?:identity|email|account|you)|enter (?:the |your )?(?:verification |security |authentication )?(?:code|pin)|two[- ](?:step|factor) (?:verification|authentication)|create (?:a |your )?(?:new )?password|reset (?:your )?password)(?:\b|$)/i.test(label(element)));
  if (challenge.length || challengeLabels) return {...result, stage: 'verification'};
  const errors = select('[role="alert"], .error-message, .error__message, .form-error, [data-testid="error-message"]');
  if (errors.some(element => /(?:something went wrong|try again later|incorrect|invalid|not recogni[sz]ed|vpn|ad.blocker|locked|too many|unable to sign in)/i.test(label(element)))) {
    return {...result, stage: 'error'};
  }
  if (document.readyState === 'loading') return {...result, stage: 'loading'};

  let field = null, advance = null;
  const accountOrigin = location.origin !== 'https://watch.sportsnet.ca';
  if (accountOrigin) {
    const passwords = select('input[type="password"]');
    const emails = select('input[type="email"], input[autocomplete="username"], input[name="email"], input[name="username"]')
      .filter(element => element.type !== 'password');
    if (passwords.length === 1 && emails.length <= 1 &&
        unique(exact(['sign in', 'sign in with email'])) &&
        safeForm(passwords[0], unique(exact(['sign in', 'sign in with email'])))) {
      result.stage = 'password';
      field = passwords[0];
      advance = unique(exact(['sign in', 'sign in with email']));
      result.next = 'submit';
    } else if (passwords.length === 0 && emails.length === 1 && unique(exact(['continue'])) &&
               safeForm(emails[0], unique(exact(['continue'])))) {
      result.stage = 'email';
      field = emails[0];
      advance = unique(exact(['continue']));
      result.next = 'continue';
    }
  } else {
    // Observed Sportsnet source renders this pair only for an active signed-in
    // profile. Do not infer authentication from a generic avatar or profile link.
    const signedIn = select('.account-nav--signed-in .account-nav__profile').length > 0;
    const videos = select('video').filter(video => video.readyState >= 2 && !video.ended &&
      Number.isFinite(video.currentTime) && video.currentTime >= 0 && video.currentTime <= 604800);
    const video = unique(videos);
    if (video && /^\/(?:watch|channel)\//.test(location.pathname)) {
      result.stage = video.paused ? (signedIn ? 'signed_in' : 'unknown') : 'playing';
      result.video_time = video.currentTime;
      result.paused = !!video.paused;
      result.muted = !!video.muted;
      const full = document.fullscreenElement;
      result.fullscreen = !!full && (full === video || full.contains(video));
    } else if (signedIn) {
      result.stage = 'signed_in';
    }
    const subscription = controls().filter(element => {
      const name = label(element);
      return name === 'sportsnet+' || /^sportsnet\+ sign in directly with your sportsnet\+ subscription(?:\s|$)/.test(name);
    });
    const entry = exact(['sign in', 'sign in or subscribe to watch']);
    // An actual login/entitlement prompt wins over a retained header profile.
    if (unique(subscription)) {
      result.stage = 'signin_choice';
      result.next = 'subscription';
      advance = subscription[0];
    } else if (!signedIn && entry.length) {
      result.stage = 'signin_choice';
      result.next = 'signin_entry';
      advance = entry[0];
    }
  }
  if (field) {
    // Only presence crosses the boundary; never copy or serialize the value.
    result.filled = field.value.length > 0;
    const rect = fieldRect(field);
    if (rect) result.rect = rect;
    if (!result.filled) result.next = 'autofill';
  }
  if (command.action !== 'sportsnet_step') return result;
  result.acted = false;
  if (command.expected_stage !== result.stage || !document.hasFocus()) return result;
  if (command.operation === 'field') {
    if (field && result.rect) {
      field.focus({preventScroll: true});
      result.acted = true;
    }
  } else if (command.operation === 'advance' && advance && enabled(advance) &&
             (!field || result.filled)) {
    // Element.click supplies no credentials and is scoped to the freshly
    // classified normal sign-in control. Native input handles Chrome autofill.
    advance.click();
    result.acted = true;
  }
  return result;
}
