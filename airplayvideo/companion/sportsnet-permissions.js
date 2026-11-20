'use strict';

const sportsnetOrigins = ['https://watch.sportsnet.ca', 'https://account.sportsnet.ca',
  'https://rogers-prd.transmit.security'];
const button = document.getElementById('enable');
const status = document.getElementById('status');
button.addEventListener('click', async event => {
  // Never request or accept this grant from native messaging or a background
  // detector. Chrome also requires the actual user gesture for this API.
  if (!event.isTrusted || button.disabled) return;
  button.disabled = true;
  try {
    const granted = await chrome.permissions.request({origins: sportsnetOrigins.map(origin => origin + '/*')});
    if (!granted) {
      status.textContent = 'Sportsnet sign-in access was not enabled. You can try again when ready.';
      button.disabled = false;
      return;
    }
    status.textContent = 'Sportsnet sign-in is enabled. Return to Sportsnet to continue.';
    const current = await chrome.tabs.getCurrent();
    if (!Number.isInteger(current?.id) || !Number.isInteger(current.openerTabId)) return;
    const previous = await chrome.tabs.get(current.openerTabId);
    // The former tab may have changed or closed while the grant was pending.
    // Restore only the retained Sportsnet flow, never an unrelated destination.
    if (!previous.url || previous.windowId !== current.windowId ||
        !sportsnetOrigins.includes(new URL(previous.url).origin)) return;
    await chrome.tabs.update(previous.id, {active: true});
    await chrome.tabs.remove(current.id);
  } catch (_) {
    status.textContent = 'Use Chrome’s site access prompt, then return to Sportsnet to continue.';
    button.disabled = false;
  }
});
