'use strict';
importScripts('youtube.js', 'sportsnet.js');
let nativePort;

async function activeTab() {
  const tabs = await chrome.tabs.query({active: true, lastFocusedWindow: true});
  if (tabs.length !== 1 || !Number.isInteger(tabs[0].id)) throw new Error('No active browser tab');
  return tabs[0];
}

async function command(message) {
  if (message.action === 'ready') return {ok: true, version: chrome.runtime.getManifest().version};
  const tab = await activeTab();
  if (message.action === 'sportsnet_enable') {
    const page = chrome.runtime.getURL('sportsnet-permissions.html');
    // URL query filters can be ignored without a matching host grant. Compare
    // our own page explicitly so no unrelated tab is mistaken for this page.
    const existing = (await chrome.tabs.query({windowId: tab.windowId})).filter(item => item.url === page);
    if (existing.length && Number.isInteger(existing[0].id)) {
      await chrome.tabs.update(existing[0].id, {active: true});
    } else {
      await chrome.tabs.create({windowId: tab.windowId, openerTabId: tab.id, url: page, active: true});
    }
  } else if (message.action === 'navigate') {
    const url = new URL(message.url);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw new Error('Invalid URL');
    if (message.new_tab === true) {
      // Updating the initial blank tab deliberately retains Chrome's omnibox
      // focus. A new foreground tab receives native web-contents focus.
      // Preserve every pre-existing tab; only Browser's cold request creates.
      const created = await chrome.tabs.create({windowId: tab.windowId, url: url.href, active: true});
      if (!Number.isInteger(created.id)) throw new Error('Browser tab did not open');
    } else {
      await chrome.tabs.update(tab.id, {url: url.href});
    }
  } else if (message.action === 'back') {
    await chrome.tabs.goBack(tab.id);
  } else if (message.action === 'forward') {
    await chrome.tabs.goForward(tab.id);
  } else if (message.action === 'reload') {
    await chrome.tabs.reload(tab.id);
  } else if (message.action === 'close') {
    // Acknowledge before closing the window and its native-messaging connection.
    setTimeout(() => chrome.windows.remove(tab.windowId).catch(() => {}), 50);
  } else if (['sportsnet_status', 'sportsnet_step'].includes(message.action)) {
    const origins = ['https://watch.sportsnet.ca', 'https://account.sportsnet.ca', 'https://rogers-prd.transmit.security'];
    // Optional origins preserve the existing companion's permissions during an
    // upgrade. Only the extension page's actual user click requests this grant.
    if (!await chrome.permissions.contains({origins: origins.map(origin => origin + '/*')})) return {stage: 'permission'};
    if (!tab.url || !origins.includes(new URL(tab.url).origin)) return {stage: 'other'};
    if (tab.status === 'loading') return {stage: 'loading'};
    if (message.action === 'sportsnet_step' &&
        (!['signin_choice', 'email', 'password'].includes(message.expected_stage) ||
         !['advance', 'field'].includes(message.operation))) throw new Error('Invalid Sportsnet control');
    const results = await chrome.scripting.executeScript({target: {tabId: tab.id}, world: 'ISOLATED',
      func: sportsnetControl, args: [{action: message.action, expected_stage: message.expected_stage,
        operation: message.operation, target_url: message.target_url}]});
    return results[0]?.result || {stage: 'unknown'};
  } else if (['youtube_prepare', 'play_pause', 'fullscreen', 'fullscreen_status'].includes(message.action)) {
    // Host grants alone make these URLs visible. There is no cookie, debugger,
    // arbitrary-page script, or broad tabs permission.
    if (message.action === 'youtube_prepare' && tab.status === 'loading') return {ready: false};
    if (!tab.url || new URL(tab.url).origin !== 'https://www.youtube.com') return {ready: false, interaction: true};
    if (!['auto', '1080p', '720p'].includes(message.quality || 'auto')) throw new Error('Invalid quality');
    const results = await chrome.scripting.executeScript({target: {tabId: tab.id}, world: 'MAIN', func: youtubeControl, args: [{action: message.action, quality: message.quality || 'auto', watch_later: message.watch_later === true}]});
    return results[0]?.result || {ready: false};
  } else {
    throw new Error('Unknown control');
  }
  return {ok: true};
}

function connect() {
  if (nativePort) return;
  const port = chrome.runtime.connectNative('com.ordinaryplace.airplayvideo');
  nativePort = port;
  port.onMessage.addListener(message => {
    if (!message || !Number.isSafeInteger(message.id)) return;
    command(message).then(result => {
      if (nativePort === port) port.postMessage({id: message.id, result});
    }).catch(() => {
      // Never return page URLs, content, account fields or raw Chrome errors.
      if (nativePort === port) port.postMessage({id: message.id, error: 'Browser control could not finish'});
    });
  });
  port.onDisconnect.addListener(() => {
    void chrome.runtime.lastError;
    if (nativePort === port) nativePort = null;
    setTimeout(connect, 2000);
  });
}
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
// The native port intentionally keeps this worker alive. Do not let that
// connection postpone a verified Chrome-managed extension update indefinitely.
chrome.runtime.onUpdateAvailable.addListener(() => chrome.runtime.reload());
connect();
