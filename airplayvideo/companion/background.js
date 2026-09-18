'use strict';
importScripts('youtube.js');
let nativePort;

async function activeTab() {
  const tabs = await chrome.tabs.query({active: true, lastFocusedWindow: true});
  if (tabs.length !== 1 || !Number.isInteger(tabs[0].id)) throw new Error('No active browser tab');
  return tabs[0];
}

async function command(message) {
  if (message.action === 'ready') return {ok: true};
  const tab = await activeTab();
  if (message.action === 'navigate') {
    const url = new URL(message.url);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw new Error('Invalid URL');
    await chrome.tabs.update(tab.id, {url: url.href});
  } else if (message.action === 'back') {
    await chrome.tabs.goBack(tab.id);
  } else if (message.action === 'forward') {
    await chrome.tabs.goForward(tab.id);
  } else if (message.action === 'reload') {
    await chrome.tabs.reload(tab.id);
  } else if (message.action === 'close') {
    // Acknowledge before closing the window and its native-messaging connection.
    setTimeout(() => chrome.windows.remove(tab.windowId).catch(() => {}), 50);
  } else if (['youtube_prepare', 'play_pause', 'fullscreen'].includes(message.action)) {
    // Only the YouTube permission makes this URL visible. There is no account,
    // cookie, debugger, arbitrary-page script, or broad tabs permission.
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
connect();
