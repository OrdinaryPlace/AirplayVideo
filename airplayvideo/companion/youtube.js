'use strict';

// Serialized by Chrome's scripting API; this function has no outer dependencies.
function youtubeControl(command) {
  if (location.origin !== 'https://www.youtube.com') return {ready: false, interaction: true};
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
  const video = document.querySelector('video');
  const player = document.querySelector('#movie_player');
  if (!video || !player || video.readyState < 2) return {ready: false};
  if (command.action === 'play_pause') {
    if (video.paused) video.play().catch(() => {}); else video.pause();
    return {ready: true};
  }
  if (command.action === 'youtube_prepare') {
    const quality = {'1080p': 'hd1080', '720p': 'hd720', auto: 'auto'}[command.quality];
    if (quality !== 'auto' && typeof player.setPlaybackQualityRange === 'function') player.setPlaybackQualityRange(quality, quality);
    video.play().catch(() => {});
  }
  // Fit the player inside the already-fullscreen Chrome window. This does not
  // forge a user gesture or modify browser automation/security indicators.
  if (!window.__airplayVideoFit) {
    const saved = new Map();
    const restore = () => {
      for (const [element, style] of saved) {
        if (style === null) element.removeAttribute('style'); else element.setAttribute('style', style);
      }
      saved.clear();
    };
    const remember = element => {if (!saved.has(element)) saved.set(element, element.getAttribute('style'));};
    const refresh = () => {
      const current = document.querySelector('#movie_player');
      const prompt = [...document.querySelectorAll('[role="dialog"][aria-modal="true"], tp-yt-paper-dialog, ytd-consent-bump-v2-lightbox')].some(element => {
        const rect = element.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
      });
      if (!current || prompt || !location.pathname.startsWith('/watch')) {restore(); return;}
      // Fixed-position video does not remove the underlying document's scroll
      // range. Hide its scrollbar only while filling video, and restore it for
      // normal navigation, sign-in and consent dialogs.
      for (const root of [document.documentElement, document.body]) {
        remember(root);
        root.style.setProperty('overflow', 'hidden', 'important');
        root.style.setProperty('scrollbar-gutter', 'auto', 'important');
      }
      for (let branch = current; branch.parentElement; branch = branch.parentElement) {
        for (const sibling of branch.parentElement.children) {
          if (sibling !== branch && sibling instanceof HTMLElement) {
            remember(sibling); sibling.style.setProperty('visibility', 'hidden', 'important');
          }
        }
        if (branch === document.body) break;
      }
      remember(current);
      for (const [property, value] of Object.entries({position: 'fixed', top: '0', left: '0', width: '100vw', height: '100vh', maxWidth: 'none', maxHeight: 'none', zIndex: '2147483000'})) {
        const cssName = property.replace(/[A-Z]/g, c => '-' + c.toLowerCase());
        current.style.setProperty(cssName, value, 'important');
      }
      window.dispatchEvent(new Event('resize'));
    };
    window.__airplayVideoFit = {refresh};
    setInterval(refresh, 1000);
  }
  window.__airplayVideoFit.refresh();
  return {ready: true};
}
