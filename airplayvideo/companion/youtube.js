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
  // Fullscreen is owned by the site and browser. This only authorizes YouTube's
  // native shortcut; trusted X input supplies the required user gesture.
  const full = document.fullscreenElement;
  const rect = player.getBoundingClientRect();
  const fullscreen = !!full && (full === video || full.contains(video)) &&
    Math.abs(rect.left) <= 2 && Math.abs(rect.top) <= 2 &&
    Math.abs(rect.width - innerWidth) <= 2 && Math.abs(rect.height - innerHeight) <= 2;
  if (command.action === 'fullscreen_status') return {ready: true, fullscreen};
  if (command.action !== 'fullscreen') return {ready: false};
  if (fullscreen) return {ready: true, fullscreen: true};
  // Do not type into Chrome's address bar or an embedded frame. The editable
  // focus guard above also applies when the page has a search/comment field.
  if (!document.fullscreenEnabled || !document.hasFocus() || active?.matches('iframe')) return {ready: false, interaction: true};
  return {ready: true, fullscreen: false, shortcut: 'f'};
}
