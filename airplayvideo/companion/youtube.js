'use strict';

// Serialized by Chrome's scripting API; this function has no outer dependencies.
function youtubeControl(command) {
  if (location.origin !== 'https://www.youtube.com') return {ready: false, interaction: true};
  const visible = element => {
    if (!element || !element.isConnected) return false;
    const rect = element.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return false;
    for (let parent = element; parent; parent = parent.parentElement) {
      const style = getComputedStyle(parent);
      if (style.display === 'none' || style.visibility !== 'visible' || Number(style.opacity) === 0) return false;
    }
    return true;
  };
  const prompt = [...document.querySelectorAll('[role="dialog"][aria-modal="true"], tp-yt-paper-dialog, ytd-consent-bump-v2-lightbox')].some(visible);
  if (prompt) return {ready: false, interaction: true};
  if (command.watch_later && location.pathname === '/playlist') {
    const rows = [...document.querySelectorAll('ytd-playlist-video-renderer')];
    const next = rows.find(row => {
      const progress = row.querySelector('#progress');
      return !progress || parseFloat(progress.style.width) < 100;
    });
    const link = next?.querySelector('a#video-title');
    if (link) {
      const target = new URL(link.href, location.href);
      if (target.origin === location.origin && target.pathname === '/watch') link.click();
    }
    return {ready: false};
  }
  if (location.pathname !== '/watch') return {ready: false, interaction: true};
  const player = document.querySelector('#movie_player');
  const video = player?.querySelector('video');
  if (!video || video.readyState < 2 || !visible(player)) return {ready: false};
  if (command.action === 'play_pause') {
    if (video.paused) video.play().catch(() => {}); else video.pause();
    return {ready: true};
  }
  const active = document.activeElement;
  if (active && (active.matches('input, textarea, select') || active.isContentEditable)) return {ready: false, interaction: true};
  if (command.action === 'youtube_prepare') {
    const quality = {'1080p': 'hd1080', '720p': 'hd720', auto: 'auto'}[command.quality];
    if (quality !== 'auto' && typeof player.setPlaybackQualityRange === 'function') player.setPlaybackQualityRange(quality, quality);
    video.play().catch(() => {});
    return {ready: true};
  }
  // Fullscreen is owned by the site and browser. This only reports a verified
  // native control's position; trusted X input supplies the required gesture.
  const full = document.fullscreenElement;
  const rect = player.getBoundingClientRect();
  const fullscreen = !!full && (full === video || full.contains(video)) &&
    Math.abs(rect.left) <= 2 && Math.abs(rect.top) <= 2 &&
    Math.abs(rect.width - innerWidth) <= 2 && Math.abs(rect.height - innerHeight) <= 2;
  if (command.action === 'fullscreen_status') return {ready: true, fullscreen};
  if (command.action !== 'fullscreen') return {ready: false};
  if (fullscreen) return {ready: true, fullscreen: true};
  if (!document.fullscreenEnabled) return {ready: false, interaction: true};
  const scale = window.devicePixelRatio;
  const viewport = {width: Math.round(innerWidth * scale), height: Math.round(innerHeight * scale)};
  const center = element => {
    const bounds = element.getBoundingClientRect();
    const x = bounds.left + bounds.width / 2, y = bounds.top + bounds.height / 2;
    if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight || !element.contains(document.elementFromPoint(x, y))) return null;
    return {x: Math.round(x * scale), y: Math.round(y * scale)};
  };
  const button = player.querySelector('button.ytp-fullscreen-button');
  if (visible(button) && !button.disabled && button.getAttribute('aria-disabled') !== 'true') {
    const click = center(button);
    return click ? {ready: true, fullscreen: false, viewport, click} : {ready: false, interaction: true};
  }
  // Hidden controls can be revealed by an owned pointer move, followed by a
  // fresh control lookup. Never click the player or guess the button location.
  const reveal = center(player);
  return reveal ? {ready: true, fullscreen: false, viewport, reveal} : {ready: false, interaction: true};
}
