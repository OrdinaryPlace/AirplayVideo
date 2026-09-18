import RFB from '../novnc/core/rfb.js';

const $ = id => document.getElementById(id);
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let state, draft, step = 0, mode = 'browser', busy = false;
let chosenTVs = new Set(), selectionDirty = false, discoveredTVs = [], discoveredTuners = [];
let rfb = null, previewConnecting = false, previewWanted = false;
const stepNames = ['Sources', 'Configure', 'Pair TVs', 'Picture & sound', 'Finish'];
const titles = ['Choose your sources', 'Configure your sources', 'Pair your TVs', 'Set picture and sound', 'Connect Home Assistant'];
const descriptions = [
  'Enable the modes you want. You can return to Setup whenever your installation changes.',
  'Set defaults here so everyday playback only needs a page or channel.',
  'Each TV uses its own saved AirPlay pairing. Enter the code displayed on that TV.',
  'These settings apply to the shared stream on every selected TV.',
  'Create everyday playback controls for dashboards and automations.'
];

async function api(path, body) {
  const options = body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json','X-AirplayVideo':'1'}, body:JSON.stringify(body)};
  const response = await fetch(path, options);
  let result;
  try { result = await response.json(); } catch { throw new Error('The app connection ended. Refresh after it is running.'); }
  if (!response.ok) throw new Error(result.error || 'The operation could not be completed.');
  return result;
}

function message(error = '', notice = '') {
  $('error').textContent = error; $('error').hidden = !error;
  $('notice').textContent = notice; $('notice').hidden = !notice;
}

async function refresh() {
  state = await api('api/state');
  $('loading').hidden = true;
  $('version').textContent = state.version;
  $('connection').textContent = state.home_assistant.connected ? 'Home Assistant connected' : 'App controls ready';
  if (!selectionDirty) chosenTVs = new Set(state.runtime.targets);
  renderUsage();
  return state;
}

function setOptions(select, rows, empty) {
  const signature = JSON.stringify([rows, empty]);
  if(select.dataset.signature === signature) return;
  select.dataset.signature = signature;
  const current = select.value;
  select.replaceChildren();
  for (const row of rows) {
    const option = new Option(row.label, row.value);
    option.disabled = !!row.disabled;
    select.add(option);
  }
  if (!rows.length) select.add(new Option(empty, ''));
  if (rows.some(row => row.value === current)) select.value = current;
}

function route() {
  if (!state) return;
  const setup = !state.setup.complete || location.hash.startsWith('#setup');
  $('wizard').hidden = !setup; $('playback').hidden = setup;
  $('setupTab').classList.toggle('active', setup); $('playTab').classList.toggle('active', !setup);
  if (setup) {
    if (!draft) draft = structuredClone(state.setup);
    renderStep();
  } else renderUsage();
}

function openSetup(target = 0) {
  draft = structuredClone(state.setup); step = target;
  location.hash = '#setup'; route();
  if (state.runtime.targets.length || state.runtime.phase === 'preparing')
    message('', 'Stop playback before saving changes to Setup.');
}

function collectStep() {
  if (step === 0) {
    draft.modes.browser = $('enableBrowser').checked;
    draft.modes.hdhomerun = $('enableHDHR').checked;
    if (!draft.modes.browser && !draft.modes.hdhomerun) throw new Error('Enable at least one source mode.');
  } else if (step === 1) {
    if ($('homeUrl')) draft.browser.home_url = $('homeUrl').value;
    if ($('youtubeQuality')) draft.browser.youtube_quality = $('youtubeQuality').value;
    if (draft.modes.hdhomerun && !draft.hdhomerun.devices.length) throw new Error('Find or add your HDHomeRun first.');
  } else if (step === 3) {
    draft.video.resolution = $('resolution').value;
    draft.video.fps = Number($('frameRate').value);
    draft.video.codec = 'h264';
    draft.video.encoder = $('encoder').value;
    draft.video.bitrate_mbps = Number($('bitrate').value);
    draft.video.deinterlace = $('deinterlace').checked;
    draft.audio.enabled = $('enableAudio').checked;
    draft.audio.latency_ms = Number($('latency').value);
  } else if (step === 4) draft.home_assistant.enabled = $('enableHA').checked;
}

function checked(value) { return value ? 'checked' : ''; }
function selected(value, expected) { return value === expected ? 'selected' : ''; }

function renderStep() {
  $('wizardTitle').textContent = titles[step]; $('wizardDescription').textContent = descriptions[step];
  $('stepCount').textContent = `${step + 1} of 5`;
  $('steps').innerHTML = stepNames.map((name, i) => `<li class="${i === step ? 'current' : i < step ? 'done' : ''}" ${i === step ? 'aria-current="step"' : ''}>${i + 1}. ${name}</li>`).join('');
  $('previous').hidden = step === 0;
  $('next').textContent = step === 4 ? 'Save setup & open playback' : 'Continue';
  if (step === 0) {
    $('stepContent').innerHTML = `<div class="mode-cards">
      <label class="mode-card"><input id="enableBrowser" type="checkbox" ${checked(draft.modes.browser)}><div><h2>Web browser</h2><p>Show a dashboard, saved page, or YouTube video. Sign in and interact through the browser preview.</p></div></label>
      <label class="mode-card"><input id="enableHDHR" type="checkbox" ${checked(draft.modes.hdhomerun)}><div><h2>HDHomeRun live TV</h2><p>Choose an antenna channel. One tuner supplies the same program to all selected TVs.</p></div></label>
    </div><p class="muted">Both modes can be enabled. Playback uses one source at a time.</p>`;
  } else if (step === 1) {
    $('stepContent').innerHTML = `${draft.modes.browser ? `<section class="setup-section"><h2>Browser defaults</h2><label for="homeUrl">Home page</label><input id="homeUrl" type="url" required value="${escape(draft.browser.home_url)}"><label for="youtubeQuality">YouTube quality preference</label><select id="youtubeQuality"><option value="1080p" ${selected(draft.browser.youtube_quality,'1080p')}>1080p</option><option value="720p" ${selected(draft.browser.youtube_quality,'720p')}>720p</option><option value="auto" ${selected(draft.browser.youtube_quality,'auto')}>YouTube automatic</option></select><p class="muted">The browser keeps its own sign-ins and site data. Available video quality depends on the source.</p></section>` : ''}
      ${draft.modes.hdhomerun ? `<section class="setup-section"><div class="card-header"><h2>HDHomeRun devices</h2><button id="findTuners" type="button">Find HDHomeRun</button></div><p class="muted">Discovery reads the channel list. It does not start a stream or run an antenna scan.</p><div id="configuredTuners" class="device-list"></div><div id="foundTuners" class="device-list"></div><details><summary>Enter a device address</summary><div class="inline-form"><div><label for="tunerAddress">HDHomeRun IPv4 address</label><input id="tunerAddress" placeholder="192.168.x.x" inputmode="decimal"></div><button id="probeTuner" type="button">Connect</button></div></details></section>` : ''}`;
    if (draft.modes.hdhomerun) {
      renderTuners();
      $('findTuners').onclick = () => setupTask(async () => {
        discoveredTuners = (await api('api/setup/discover_tuners', {})).devices;
        renderTuners();
        if (!discoveredTuners.length) message('', 'No devices answered discovery. Use “Enter a device address” below.');
      }, $('findTuners'));
      $('probeTuner').onclick = () => setupTask(async () => {
        const device = (await api('api/setup/probe_tuner', {address:$('tunerAddress').value.trim()})).device;
        addTuner(device); $('tunerAddress').value = ''; message('', 'HDHomeRun verified.');
      }, $('probeTuner'));
    }
  } else if (step === 2) {
    $('stepContent').innerHTML = `<div class="card-header"><h2>Your TVs</h2><button id="findTVs" type="button">Find AirPlay TVs</button></div><div id="pairedTVs" class="device-list"></div><div id="foundTVs" class="device-list"></div><details><summary>Enter a TV address</summary><div class="inline-form"><div><label for="tvAddress">Apple TV IPv4 address</label><input id="tvAddress" placeholder="192.168.x.x" inputmode="decimal"></div><div style="max-width:110px"><label for="tvPort">AirPlay port</label><input id="tvPort" type="number" value="7000" min="1" max="65535"></div><button id="probeTV" type="button">Find TV</button></div></details><p class="muted">Pair once with the on-screen code. Saved pairings survive app restarts. You can also finish Setup and add TVs later.</p>`;
    renderTVSetup();
    $('findTVs').onclick = () => setupTask(async () => {
      discoveredTVs = (await api('api/setup/discover_tvs', {})).receivers;
      renderTVSetup();
      if (!discoveredTVs.length) message('', 'No TVs answered discovery. Use “Enter a TV address” below.');
    }, $('findTVs'));
    $('probeTV').onclick = () => setupTask(async () => {
      const receiver = (await api('api/setup/probe_tv', {address:$('tvAddress').value.trim(), port:Number($('tvPort').value)})).receiver;
      discoveredTVs = [receiver]; renderTVSetup();
    }, $('probeTV'));
  } else if (step === 3) {
    const hardware = state.capabilities.encoders.includes('h264_vaapi');
    $('stepContent').innerHTML = `<div class="setup-grid">
      <div><label for="resolution">Output resolution</label><select id="resolution"><option value="1080p" ${selected(draft.video.resolution,'1080p')}>1080p · 1920 × 1080</option><option value="720p" ${selected(draft.video.resolution,'720p')}>720p · 1280 × 720</option></select></div>
      <div><label for="frameRate">Frame rate</label><select id="frameRate"><option value="30" ${selected(draft.video.fps,30)}>30 fps</option><option value="60" ${selected(draft.video.fps,60)}>60 fps</option></select></div>
      <div><label for="codec">Video codec</label><select id="codec" disabled><option>H.264 · Apple TV compatible</option></select></div>
      <div><label for="encoder">Encoding</label><select id="encoder"><option value="auto" ${selected(draft.video.encoder,'auto')}>Automatic</option><option value="libopenh264" ${selected(draft.video.encoder,'libopenh264')}>Software · OpenH264</option><option value="h264_vaapi" ${selected(draft.video.encoder,'h264_vaapi')} ${hardware?'':'disabled'}>Hardware · VAAPI ${hardware?'':'(unavailable)'}</option></select></div>
      <div><label for="bitrate">Video bitrate · <span id="bitrateValue">${draft.video.bitrate_mbps}</span> Mbps</label><input id="bitrate" type="range" min="2" max="20" step="1" value="${draft.video.bitrate_mbps}"></div>
      <div><label for="latency">Playback buffer</label><select id="latency">${[500,750,1000,1500,2000].map(value=>`<option value="${value}" ${selected(draft.audio.latency_ms,value)}>${value} ms${value===1500?' · recommended for live TV':''}</option>`).join('')}</select></div>
    </div><label class="check-row"><input id="enableAudio" type="checkbox" ${checked(draft.audio.enabled)}> Send sound to the TVs</label><label class="check-row"><input id="deinterlace" type="checkbox" ${checked(draft.video.deinterlace)}> Deinterlace broadcast video when needed</label><p class="muted">Start with 1080p at 30 fps. A higher frame rate or bitrate needs more processing and network capacity. A longer buffer gives live broadcasts more time to arrive.</p>`;
    $('bitrate').oninput = () => $('bitrateValue').textContent = $('bitrate').value;
  } else {
    const connected = state.home_assistant.connected;
    $('stepContent').innerHTML = `<label class="check-row"><input id="enableHA" type="checkbox" ${checked(draft.home_assistant.enabled)}> Create TV controls in Home Assistant</label><p class="muted">Each paired TV gets Play and Stop buttons, saved-page shortcuts, channel selection, and status for your dashboards and automations.</p><div class="integration"><strong>${connected ? 'Home Assistant connection ready' : 'Home Assistant connection needs attention'}</strong><p class="muted">${connected ? 'The app found the MQTT service automatically. Controls will appear when you finish Setup.' : escape(state.home_assistant.error || 'Enable MQTT in Home Assistant to create playback entities. You can still use the app directly.')}</p><button type="button" id="retryHA" class="text-button">Check connection again</button></div><dl class="review"><dt>Modes</dt><dd>${Object.entries(draft.modes).filter(([,enabled])=>enabled).map(([key])=>key==='browser'?'Web browser':'HDHomeRun').join(' + ')}</dd><dt>Paired TVs</dt><dd>${state.receivers.length ? escape(state.receivers.map(r=>r.name).join(', ')) : 'None yet — add them in Setup later'}</dd><dt>Picture</dt><dd>${draft.video.resolution} · ${draft.video.fps} fps · H.264</dd><dt>Sound</dt><dd>${draft.audio.enabled ? 'Stereo audio enabled' : 'Video only'}</dd><dt>After saving</dt><dd>Playback stays idle until you press Play.</dd></dl>`;
    $('retryHA').onclick = () => setupTask(async () => { await api('api/setup/home_assistant', {}); await refresh(); renderStep(); }, $('retryHA'));
  }
}

function addTuner(device) {
  if (!draft.hdhomerun.devices.some(d=>d.id===device.id)) draft.hdhomerun.devices.push(device);
  renderTuners();
}

function renderTuners() {
  $('configuredTuners').innerHTML = draft.hdhomerun.devices.map((device,index)=>`<div class="device-row"><div><p>${escape(device.name)}</p><small>${escape(device.address)}</small></div><button type="button" data-remove-tuner="${index}">Remove</button></div>`).join('');
  for (const button of document.querySelectorAll('[data-remove-tuner]')) button.onclick=()=>{draft.hdhomerun.devices.splice(Number(button.dataset.removeTuner),1);renderTuners();};
  const configured = new Set(draft.hdhomerun.devices.map(d=>d.id));
  $('foundTuners').innerHTML = discoveredTuners.filter(d=>!configured.has(d.id)).map(device=>`<div class="device-row"><div><p>${escape(device.name)}</p><small>${escape(device.address)}</small></div><button type="button" data-add-tuner="${escape(device.id)}">Use device</button></div>`).join('');
  for (const button of document.querySelectorAll('[data-add-tuner]')) button.onclick=()=>addTuner(discoveredTuners.find(d=>d.id===button.dataset.addTuner));
}

function renderTVSetup() {
  $('pairedTVs').innerHTML = state.receivers.length ? state.receivers.map(receiver=>`<div class="device-row"><div><p>${escape(receiver.name)}</p><small>${escape(receiver.address)}</small></div><span class="badge">Paired</span></div>`).join('') : '<p class="empty">No TVs paired yet.</p>';
  const saved = new Set(state.receivers.map(r=>r.device_id));
  $('foundTVs').innerHTML = discoveredTVs.filter(r=>!saved.has(r.device_id)).map((receiver,index)=>`<div class="device-row"><div><p>${escape(receiver.name)}</p><small>${escape(receiver.address)} · ${escape(receiver.model)}</small></div><button type="button" data-pair-address="${escape(receiver.address)}">Show code on TV</button></div>`).join('');
  for (const button of document.querySelectorAll('[data-pair-address]')) button.onclick=()=>setupTask(async()=>{
    const receiver = discoveredTVs.find(r=>r.address===button.dataset.pairAddress);
    const result = await api('api/setup/pair_start', {receiver});
    $('pairTitle').textContent = 'Pair ' + result.name;
    $('pin').value = ''; $('pairError').hidden = true;
    $('pairDialog').showModal(); $('pin').focus();
  },button);
}

async function setupTask(action, button) {
  message(); if (button) button.disabled=true;
  try { await action(); } catch(error) { message(error.message); }
  finally { if(button) button.disabled=false; }
}

$('setupForm').onsubmit = async event => {
  event.preventDefault();
  await setupTask(async()=>{
    collectStep();
    if(step<4) {step++;renderStep();return;}
    state=await api('api/setup/save',draft); draft=null;
    location.hash='#play'; route(); message('', 'Setup saved. Choose a source and TV to begin.');
  },$('next'));
};
$('previous').onclick=()=>{try{collectStep();}catch{}step=Math.max(0,step-1);renderStep();message();};
$('pairForm').onsubmit=async event=>{
  event.preventDefault(); $('completePair').disabled=true;
  const pin=$('pin').value; $('pin').value='';
  try {await api('api/setup/pair_finish',{pin});await refresh();$('pairDialog').close();renderTVSetup();message('', 'TV paired. It will not start playing until you press Play.');}
  catch(error){$('pairError').textContent=error.message;$('pairError').hidden=false;}
  finally{$('completePair').disabled=false;}
};
async function cancelPair(){ $('pin').value='';$('pairDialog').close();try{await api('api/setup/pair_cancel',{});}catch(error){message(error.message);} }
$('cancelPair').onclick=cancelPair;
$('pairDialog').addEventListener('cancel',event=>{event.preventDefault();cancelPair();});

function requestSource(){
  if(mode==='hdhomerun')return {mode,channel:$('channelSelect').value};
  return {mode:'browser',browser_source:$('browserKind').value,page:$('pageSelect').value,url:$('sourceUrl').value.trim()};
}

function renderChannels(){
  const search=$('channelSearch').value.toLowerCase();
  const favorites=new Set(state.favorites);
  const rows=state.channels.filter(row=>(!$('favoritesOnly').checked||favorites.has(row.id))&&row.label.toLowerCase().includes(search));
  setOptions($('channelSelect'),rows.map(row=>({value:row.id,label:row.label+(row.supported?'':` — ${row.reason}`),disabled:!row.supported})),'No matching channels');
  $('favorite').textContent=favorites.has($('channelSelect').value)?'★':'☆';
  $('channelHint').textContent=state.channel_error || (!state.channels.length?'No channels loaded. Check HDHomeRun in Setup.':'Choose a channel, then Play. Selection alone does not use a tuner.');
}

function renderUsage(){
  if(!state)return;
  if(!state.setup.modes[mode])mode=state.setup.modes.browser?'browser':'hdhomerun';
  $('modeSwitch').innerHTML=Object.entries(state.setup.modes).filter(([,enabled])=>enabled).map(([kind])=>`<button class="${mode===kind?'selected':''}" data-mode="${kind}">${kind==='browser'?'Web browser':'Live TV'}</button>`).join('');
  $('modeSwitch').hidden=Object.values(state.setup.modes).filter(Boolean).length<2;
  for(const button of document.querySelectorAll('[data-mode]'))button.onclick=()=>{mode=button.dataset.mode;renderUsage();};
  $('browserSource').hidden=mode!=='browser';$('channelSource').hidden=mode!=='hdhomerun';
  setOptions($('pageSelect'),state.pages.map(page=>({value:page.id,label:page.name})),'Add a saved page');
  browserKindChanged();renderChannels();
  const runtime=state.runtime;
  const label={idle:'Idle',preparing:'Preparing',connecting:'Connecting',playing:'Sending',error:'Needs attention'}[runtime.phase]||runtime.phase;
  $('runtimeBadge').textContent=label;
  $('nowPlaying').textContent=runtime.source?`${runtime.source.label} · ${runtime.targets.length} TV${runtime.targets.length===1?'':'s'}`:'Choose a source and one or more TVs.';
  if(runtime.error&&!$('error').textContent)message(runtime.error);
  $('tvList').innerHTML=state.receivers.length?state.receivers.map(receiver=>{
    const status=runtime.receivers[receiver.id]||{};
    const description=status.message || ({streaming:'Sending',connecting:'Connecting…',stopped:'Stopped',error:'Needs attention'}[status.state]||'Ready');
    return `<div class="tv-row"><input type="checkbox" id="tv-${receiver.id}" data-tv="${receiver.id}" ${checked(chosenTVs.has(receiver.id))}><label for="tv-${receiver.id}">${escape(receiver.name)}<small>${escape(description)}</small></label>${runtime.targets.includes(receiver.id)?`<button data-stop="${receiver.id}">Stop</button>`:''}</div>`;
  }).join(''):'<p class="empty">Add your TVs in Setup, then choose where to play.</p>';
  for(const checkbox of document.querySelectorAll('[data-tv]'))checkbox.onchange=()=>{selectionDirty=true;checkbox.checked?chosenTVs.add(checkbox.dataset.tv):chosenTVs.delete(checkbox.dataset.tv);updateButtons();};
  for(const button of document.querySelectorAll('[data-stop]'))button.onclick=()=>act('stop',{receiver:button.dataset.stop},true);
  $('previewSection').hidden=mode!=='browser'||!runtime.browser_open;
  previewWanted=mode==='browser'&&runtime.browser_open&&!$('playback').hidden;
  if(previewWanted&&!rfb&&!previewConnecting)connectPreview();
  if(!previewWanted&&rfb){rfb.disconnect();rfb=null;}
  updateButtons();
}

function browserKindChanged(){
  const kind=$('browserKind').value;
  $('savedPageFields').hidden=kind!=='page';$('urlFields').hidden=!['youtube','url'].includes(kind);$('watchLaterHelp').hidden=kind!=='watch_later';
  $('urlLabel').textContent=kind==='youtube'?'YouTube video link':'Web address';
  $('sourceUrl').placeholder=kind==='youtube'?'https://www.youtube.com/watch?v=…':'https://…';
  updateButtons();
}

function updateButtons(){
  if(!state)return;
  $('play').disabled=busy||!chosenTVs.size||!state.setup.complete;
  $('play').textContent=busy?'Starting…':'Play on selected TVs';
  $('openBrowser').disabled=busy;
  $('stopAll').disabled=!busy&&!state.runtime.targets.length&&!['preparing','connecting'].includes(state.runtime.phase);
}

async function act(action,body={},interrupt=false){
  message();
  if(!interrupt){busy=true;updateButtons();}
  try{
    state=await api(`api/actions/${action}`,body);
    if(action==='play'){selectionDirty=false;chosenTVs=new Set(state.runtime.targets);}
    if(action==='stop'){if(body.receiver)chosenTVs.delete(body.receiver);else chosenTVs.clear();selectionDirty=false;}
    renderUsage();
  }catch(error){message(error.message);await refresh().catch(()=>{});}
  finally{if(!interrupt){busy=false;updateButtons();}}
}

async function connectPreview(){
  previewConnecting=true;$('previewStatus').textContent='Connecting preview…';
  try{
    const {ticket}=await api('api/preview-ticket');
    if(!previewWanted)return;
    const url=new URL('preview',location.href);url.protocol=location.protocol==='https:'?'wss:':'ws:';url.searchParams.set('ticket',ticket);url.hash='';
    const connection=new RFB($('preview'),url.href,{shared:true});rfb=connection;
    connection.scaleViewport=true;connection.resizeSession=false;connection.background='#101820';
    connection.addEventListener('connect',()=>{$('previewStatus').textContent='Click the preview to interact. Sign in to websites here.';});
    connection.addEventListener('disconnect',()=>{if(rfb===connection)rfb=null;$('previewStatus').textContent=previewWanted?'Preview disconnected. Reconnecting…':'';});
  }catch(error){$('previewStatus').textContent=error.message;}
  finally{previewConnecting=false;}
}

$('play').onclick=()=>act('play',{...requestSource(),receivers:[...chosenTVs]});
$('stopAll').onclick=()=>act('stop',{},true);
$('openBrowser').onclick=()=>act('open_browser',requestSource());
$('closeBrowser').onclick=()=>act('close_browser');
$('browserKind').onchange=browserKindChanged;
$('channelSearch').oninput=renderChannels;$('favoritesOnly').onchange=renderChannels;
$('channelSelect').onchange=()=>{$('favorite').textContent=state.favorites.includes($('channelSelect').value)?'★':'☆';};
$('favorite').onclick=()=>act('favorite',{channel:$('channelSelect').value});
$('refreshChannels').onclick=()=>act('refresh_channels');
$('setupTab').onclick=()=>openSetup();$('tvSetup').onclick=()=>openSetup(2);
$('playTab').onclick=()=>{location.hash=state.setup.complete?'#play':'#setup';route();};
for(const button of document.querySelectorAll('[data-browser]'))button.onclick=()=>act('browser',{action:button.dataset.browser});
$('expand').onclick=async()=>{if(document.fullscreenElement)await document.exitFullscreen();else await $('previewSection').requestFullscreen();};
$('paste').onclick=()=>{$('pasteText').value='';$('pasteDialog').showModal();$('pasteText').focus();};
$('cancelPaste').onclick=()=>{$('pasteText').value='';$('pasteDialog').close();};
$('pasteDialog').addEventListener('cancel',()=>{$('pasteText').value='';});
$('pasteForm').onsubmit=async event=>{event.preventDefault();const text=$('pasteText').value;$('pasteText').value='';$('pasteDialog').close();await act('browser',{action:'paste',text});};

function renderPages(){
  $('pagesList').innerHTML=state.pages.map(page=>`<div class="saved-row"><span>${escape(page.name)}</span><button type="button" data-edit-page="${page.id}">Edit</button><button type="button" data-remove-page="${page.id}">Remove</button></div>`).join('');
  for(const button of document.querySelectorAll('[data-edit-page]'))button.onclick=()=>{const page=state.pages.find(p=>p.id===button.dataset.editPage);$('pageId').value=page.id;$('pageName').value=page.name;$('pageUrl').value=page.url;};
  for(const button of document.querySelectorAll('[data-remove-page]'))button.onclick=()=>setupTask(async()=>{state=await api('api/pages/remove',{id:button.dataset.removePage});renderPages();renderUsage();},button);
}
$('managePages').onclick=()=>{renderPages();$('pageId').value='';$('pageName').value='';$('pageUrl').value='';$('pagesDialog').showModal();};
$('closePages').onclick=()=>$('pagesDialog').close();
$('pageForm').onsubmit=async event=>{event.preventDefault();await setupTask(async()=>{state=await api('api/pages/save',{id:$('pageId').value||undefined,name:$('pageName').value,url:$('pageUrl').value});$('pageId').value='';$('pageName').value='';$('pageUrl').value='';renderPages();renderUsage();});};
window.addEventListener('hashchange',route);
await refresh().then(route).catch(error=>message(error.message));
setInterval(()=>refresh().catch(error=>{$('connection').textContent='App unavailable';message(error.message);}),2000);
