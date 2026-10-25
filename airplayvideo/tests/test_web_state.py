"""Exercise playback form and recovery rendering without a live browser or TV."""
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


SOURCE = (Path(__file__).resolve().parents[1] / 'web' / 'app.js').read_text()


def run_ui(script):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for browser state checks')
    functions = []
    for name in ('message', 'renderRuntimeError', 'stableMarkup', 'renderBrowserStatus', 'renderBrowserVpn', 'renderCapitals', 'browserKindChanged', 'renderUsage'):
        match = re.search(rf'^function {name}\([^\n]*\)\s*\{{.*?^\}}', SOURCE, re.M | re.S)
        assert match, name
        functions.append(match.group())
    harness = """
const assert = require('node:assert/strict');
const nodes = new Map();
const $ = id => {
  if(!nodes.has(id)) nodes.set(id, {value:'', textContent:'', hidden:false, dataset:{}});
  return nodes.get(id);
};
const document = {querySelectorAll:()=>[]};
let errorSource='', mode='browser', generatedDirty=true, generatedDefaultsSignature='';
let state={setup:{modes:{browser:true}, generated:{}}, pages:[], receivers:[],
  runtime:{phase:'idle', targets:[], receivers:{}, source:null, native:null, error:''}};
const modeNames={browser:'Web browser'};
function renderRecordings(){}
function renderFrameStages(){}
function renderChannels(){}
function renderPreview(){}
function updateButtons(){}
function setOptions(){}
"""
    result = subprocess.run([node], input=harness + '\n'.join(functions) + script,
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_runtime_recovery_clears_only_its_banner():
    run_ui("""
renderRuntimeError('Native video failed');
assert.equal($('previewError').textContent, 'Native video failed');
renderRuntimeError('Receiver disconnected');
assert.equal($('error').textContent, 'Receiver disconnected');
renderRuntimeError('');
assert.equal($('error').hidden, true);
assert.equal($('previewError').hidden, true);
// A failed request initially displays its own action error; state then owns
// that same failure so a later successful recovery can dismiss it.
message('Native video failed');
renderRuntimeError('Native video failed');
renderRuntimeError('');
assert.equal($('error').hidden, true);
message('Choose a valid URL');
renderRuntimeError('');
assert.equal($('error').textContent, 'Choose a valid URL');
renderRuntimeError('Receiver disconnected');
assert.equal($('error').textContent, 'Choose a valid URL');
""")


def test_previous_native_report_stays_available_after_browser_recovery():
    run_ui("""
state.runtime.native={phase:'failed', error:'Native video failed', preparation_stage:'source_probe', failure_stage:'source_probe'};
state.runtime.source={kind:'browser', label:'YouTube video'};
renderUsage();
assert.equal($('nativePlaybackReport').hidden, false);
assert.match($('nativePlaybackStatus').textContent, /^Previous direct playback/);
assert.match($('nativePlaybackStatus').textContent, /Direct video failed: Reading the selected source tracks/);
assert.match($('nativePlaybackStatus').textContent, /Native video failed/);
// Retaining the failed report must not manufacture a current error banner.
assert.equal($('error').textContent, '');
state.runtime.source={kind:'youtube', label:'YouTube video'};
state.runtime.native.phase='playing';
state.runtime.native.error='';
renderUsage();
assert.equal($('nativePlaybackStatus').textContent, 'Sending direct video');
""")


@pytest.mark.parametrize(('stage', 'label'), [
    ('resolve_and_capabilities', 'Resolving public video and checking media tools'),
    ('plan', 'Selecting direct video tracks'),
    ('source_probe', 'Reading the selected source tracks'),
    ('source_validation', 'Checking source quality and timing'),
    ('encoder_probe', 'Verifying the required encoder'),
    ('hls_start', 'Preparing the first media segment'),
    ('segment_validation', 'Checking the first media segment'),
    ('ready', 'Direct video is ready'),
])
def test_native_preparation_and_failure_show_safe_stage(stage, label):
    run_ui("""
state.runtime.phase='preparing';
state.runtime.source={kind:'youtube',label:'YouTube video'};
state.runtime.native={phase:'preparing',preparation_stage:""" + json.dumps(stage) + """,error:''};
renderUsage();
assert.equal($('nativePlaybackStatus').hidden, false);
assert.equal($('nativePlaybackStatus').textContent, 'Preparing direct video: '+""" + json.dumps(label) + """);
state.runtime.native.phase='failed';
state.runtime.native.failure_stage=state.runtime.native.preparation_stage;
state.runtime.native.preparation_stage='ready';
state.runtime.native.error='Direct YouTube preparation failed; source quality was not reduced.';
renderUsage();
assert.equal($('nativePlaybackStatus').textContent, 'Direct video failed: '+""" + json.dumps(label) + """+' · Direct YouTube preparation failed; source quality was not reduced.');
assert.equal($('nativePlaybackReport').hidden, false);
""")


@pytest.mark.parametrize('stage', [None, 'constructor', '__proto__', 'https://private.example/source?token=secret'])
def test_unknown_stage_never_exposes_raw_status(stage):
    run_ui("""
state.runtime.source={kind:'youtube',label:'YouTube video'};
state.runtime.native={phase:'failed',failure_stage:""" + json.dumps(stage) + """,stage:'raw private detail',error:'Native video failed'};
renderUsage();
assert.equal($('nativePlaybackStatus').textContent, 'Direct video needs attention · Native video failed');
""")


def test_delivery_changes_and_state_refresh_preserve_typed_url():
    url = 'https://www.youtube.com/watch?v=abcdefghijk'
    run_ui("""
$('browserKind').value='youtube';
$('sourceUrl').value=""" + json.dumps(url) + ";\n" + """
const input=$('sourceUrl');
for(const delivery of ['browser','native','browser']) {
  $('youtubeDelivery').value=delivery;
  browserKindChanged();
  renderUsage();
  assert.equal($('sourceUrl'), input);
  assert.equal($('sourceUrl').value,""" + json.dumps(url) + ");\n" + """
  assert.equal($('openBrowser').hidden, delivery==='native');
  assert.equal($('nativeQualityFields').hidden, delivery!=='native');
}
""")


def test_browser_fullscreen_status_is_fixed_and_clears_when_closed():
    run_ui("""
state.runtime.browser_open=true;
state.runtime.browser_status='Finish interacting with the page, then use Fullscreen video';
renderBrowserStatus();
assert.equal($('browserVideoStatus').hidden, false);
assert.equal($('browserVideoStatus').textContent, state.runtime.browser_status);
state.runtime.browser_status='Opened YouTube fullscreen';
renderBrowserStatus();
assert.equal($('browserVideoStatus').textContent, 'Opened YouTube fullscreen');
state.runtime.browser_status='<script>private page details</script>';
renderBrowserStatus();
assert.equal($('browserVideoStatus').hidden, true);
state.runtime.browser_status='Opened YouTube fullscreen';
state.runtime.browser_open=false;
renderBrowserStatus();
assert.equal($('browserVideoStatus').hidden, true);
""")


def test_unreachable_vpn_never_looks_like_a_direct_connection():
    run_ui("""
state.browser_vpn={name:'Montréal VPN',configured:true,enabled:true,gateway_reachable:false};
renderBrowserVpn();
assert.equal($('browserVpnControl').hidden, false);
assert.equal($('browserVpnName').textContent, 'Montréal VPN');
assert.equal($('browserVpnEnabled').checked, true);
assert.equal($('browserVpnRoute').textContent, 'On');
assert.match($('browserVpnHelp').textContent, /unavailable/);
assert.match($('browserVpnHelp').textContent, /remains on the VPN route/);
state.browser_vpn.enabled=false;
renderBrowserVpn();
assert.equal($('browserVpnEnabled').checked, false);
assert.equal($('browserVpnRoute').textContent, 'Off');
assert.equal($('browserVpnHelp').textContent, 'Browser connects directly.');
state.browser_vpn.configured=false;
renderBrowserVpn();
assert.equal($('browserVpnControl').hidden, true);
""")


def run_recording_ui(script):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for browser state checks')
    functions = []
    for name in ('cadenceNumber', 'frameStageBlockReason', 'frameStagesMarkup', 'renderFrameStages', 'pictureCadenceMarkup', 'stableMarkup', 'renderRecordings', 'updateButtons'):
        match = re.search(rf'^function {name}\([^\n]*\)\s*\{{.*?^\}}', SOURCE, re.M | re.S)
        assert match, name
        functions.append(match.group())
    harness = r"""
const assert=require('node:assert/strict');
const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const nodes=new Map();
const $=id=>{
  if(!nodes.has(id))nodes.set(id,{value:'',textContent:'',innerHTML:'',hidden:false,dataset:{},disabled:false});
  return nodes.get(id);
};
let buttons=[];
const list=$('recordingList');
Object.defineProperty(list,'innerHTML',{get(){return this.markup||'';},set(value){
  this.markup=value;
  buttons=[...value.matchAll(/<button\b([^>]*)>/g)].map(match=>{
    const attrs=match[1],dataset={};
    for(const attribute of attrs.matchAll(/data-([a-z-]+)="([^"]*)"/g)){
      const key=attribute[1].replace(/-([a-z])/g,(_,c)=>c.toUpperCase());
      dataset[key]=attribute[2];
    }
    return {dataset,disabled:/\bdisabled\b/.test(attrs)};
  });
}});
const document={querySelectorAll(selector){
  const key=selector.slice(6,-1).replace(/-([a-z])/g,(_,c)=>c.toUpperCase());
  return buttons.filter(button=>key in button.dataset);
}};
let busy=false,busyAction='',draft=null,mode='browser',chosenTVs=new Set(),calls=[];
let state={setup:{complete:true},runtime:{phase:'idle',targets:[],source:null,browser_open:true},
  diagnostics:{active:false},receivers:[],browser_vpn:{configured:true},recordings:{active:null,items:[],analysis:{status:'idle',id:null,error:''}}};
function act(...args){calls.push(args);}
function automationExample(){}
function settingsStatus(){}
const sample={id:'0123456789abcdef0123456789abcdef',status:'complete',seconds_requested:15,width:1920,height:1080};
const result={version:1,video_frames:450,duration_seconds:15,encoded_fps:30.0,
  pts:{median_ms:33.3,p95_ms:34,max_ms:66.7,gaps_over_50ms:1},regions:[
    {name:'center',thresholds:[{luma_delta:1,changed_fraction:0.005,updates:90,updates_per_second:6,longest_hold_ms:160},
      {luma_delta:4,changed_fraction:0.02,updates:45,updates_per_second:3,longest_hold_ms:320}]},
    {name:'left',thresholds:[{luma_delta:2,changed_fraction:0.02,updates:60,updates_per_second:4,longest_hold_ms:250}]},
    {name:'right',thresholds:[{luma_delta:2,changed_fraction:0.02,updates:75,updates_per_second:5,longest_hold_ms:200}]}]};
"""
    result = subprocess.run([node], input=harness + '\n'.join(functions) + script,
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_sample_analysis_is_explicit_id_only_and_never_plays_a_tv():
    run_recording_ui("""
state.recordings.items=[sample];
renderRecordings();
const button=document.querySelectorAll('[data-analyze-recording]')[0];
assert.equal(button.disabled,false);
button.onclick();
assert.deepEqual(calls,[['analyze_recording',{id:sample.id}]]);
assert.match(list.innerHTML,/Download video/);
assert.equal(list.innerHTML.includes('<video'),false);
assert.equal(list.innerHTML.includes('autoplay'),false);
busy=true;updateButtons();
assert.equal(button.disabled,true);
assert.equal(document.querySelectorAll('[data-remove-recording]')[0].disabled,true);
busy=false;updateButtons();
assert.equal(button.disabled,false);
assert.equal(document.querySelectorAll('[data-remove-recording]')[0].disabled,false);
""")


def test_picture_numbers_distinguish_encoded_capture_from_visible_changes():
    run_recording_ui(r"""
state.recordings.items=[{...sample,picture_cadence:result}];renderRecordings();
assert.match(list.innerHTML,/Encoded capture cadence/);
assert.match(list.innerHTML,/30\.0 fps/);
assert.match(list.innerHTML,/Observable picture changes/);
assert.match(list.innerHTML,/3\.0–6\.0\/s/);
assert.match(list.innerHTML,/Longest hold 320 ms/);
assert.match(list.innerHTML,/66\.7 ms/);
assert.match(list.innerHTML,/Static scenes and compression/);
assert.match(list.innerHTML,/not source frame rate or TV performance/);
""")


def test_running_analysis_cancel_targets_only_the_active_sample():
    run_recording_ui("""
state.recordings.items=[sample,{...sample,id:'abcdef0123456789abcdef0123456789'}];
state.recordings.analysis={id:sample.id,status:'running',error:''};renderRecordings();
assert.equal(document.querySelectorAll('[data-analyze-recording]').every(button=>button.disabled),true);
const cancel=document.querySelectorAll('[data-cancel-recording-analysis]');
assert.equal(cancel.length,1);cancel[0].onclick();
assert.deepEqual(calls,[['cancel_recording_analysis',{id:sample.id},true]]);
assert.match(list.innerHTML,/No TVs are started/);
assert.equal(document.querySelectorAll('[data-remove-recording]')[0].disabled,true);
updateButtons();
for(const id of ['play','recordCapture','openBrowser','browserVpnEnabled','capitalsCheck','measureSync'])assert.equal($(id).disabled,true,id);
assert.equal($('cancelSync').hidden,true);
""")


def test_error_cancel_and_saved_result_survive_state_refresh_without_html_injection():
    run_recording_ui(r"""
state.recordings.items=[{...sample,picture_cadence:result}];
state.recordings.analysis={id:sample.id,status:'error',error:'<img src=x onerror=secret>'};renderRecordings();
assert.match(list.innerHTML,/&lt;img/);
assert.equal(list.innerHTML.includes('<img'),false);
assert.match(list.innerHTML,/30\.0 fps/);
state.recordings.analysis.status='cancelled';renderRecordings();
assert.match(list.innerHTML,/Picture analysis cancelled/);
assert.equal(document.querySelectorAll('[data-cancel-recording-analysis]').length,0);
assert.equal(document.querySelectorAll('[data-analyze-recording]')[0].disabled,false);
assert.match(list.innerHTML,/30\.0 fps/);
""")


def test_invalid_numeric_and_unknown_region_fields_are_not_rendered():
    run_recording_ui(r"""
result.encoded_fps='https://private.invalid/token';result.video_frames=NaN;
result.pts.max_ms=Infinity;result.duration_seconds=-1;
result.regions.push({name:'<img private>',thresholds:[{luma_delta:1,changed_fraction:0.005,updates_per_second:999,longest_hold_ms:0}]});
result.regions[0].thresholds.push({luma_delta:8,changed_fraction:0.5,updates_per_second:999,longest_hold_ms:0});
const markup=pictureCadenceMarkup(result);
for(const text of ['private','Infinity','NaN','999'])assert.equal(markup.includes(text),false,text);
assert.match(markup,/— fps/);assert.match(markup,/3\.0–6\.0\/s/);
assert.equal(pictureCadenceMarkup({...result,version:2}),'');
""")


@pytest.mark.parametrize('condition', ['state.runtime.targets=["tv"]',
    'state.runtime.phase="connecting"', 'state.runtime.phase="stopping"',
    'state.recordings.active={id:"other"}', 'state.diagnostics.active=true'])
def test_analysis_waits_for_playback_recording_or_measurement(condition):
    run_recording_ui('state.recordings.items=[sample];' + condition + ';renderRecordings();' + """
assert.equal(document.querySelectorAll('[data-analyze-recording]')[0].disabled,true);
assert.deepEqual(calls,[]);
""")


def test_incomplete_sample_has_no_analysis_action():
    run_recording_ui("""
state.recordings.items=[{...sample,status:'error',error:'Incomplete capture'}];renderRecordings();
assert.equal(document.querySelectorAll('[data-analyze-recording]').length,0);
assert.match(list.innerHTML,/Incomplete capture/);
""")


def test_frame_stage_panel_is_collapsed_and_explicitly_off_tv():
    source = (Path(__file__).resolve().parents[1] / 'web' / 'index.html').read_text()
    match = re.search(r'<details\b[^>]*id="frameStagesPanel"[^>]*>.*?</details>', source, re.S)
    assert match and not re.search(r'\bopen\b', match.group().split('>')[0])
    assert 'Measure browser frame stages' in match.group()
    assert 'without a TV' in match.group() and 'saves no recording' in match.group()


def test_frame_stage_actions_do_not_start_browser_or_target_selected_tvs():
    run_recording_ui("""
chosenTVs=new Set(['tv1','tv2']);
renderFrameStages();
assert.deepEqual(calls,[]);
assert.equal($('measureFrameStages').disabled,false);
assert.equal($('cancelFrameStages').hidden,true);
$('measureFrameStages').onclick();
assert.deepEqual(calls,[['measure_frame_stages']]);
state.frame_probe={status:'running',error:'',report:null};renderFrameStages();
assert.equal($('measureFrameStages').disabled,true);
assert.equal($('cancelFrameStages').hidden,false);
assert.match($('frameStagesStatus').textContent,/No TVs are started and no recording is saved/);
$('cancelFrameStages').onclick();
assert.deepEqual(calls,[['measure_frame_stages'],['cancel_frame_stages',{},true]]);
updateButtons();
for(const id of ['play','recordCapture','openBrowser','closeBrowser','browserVpnEnabled','capitalsCheck','measureSync'])assert.equal($(id).disabled,true,id);
assert.equal($('cancelSync').hidden,true);
""")


@pytest.mark.parametrize('condition', [
    'state.runtime.browser_open=false', 'state.runtime.targets=["tv"]',
    'state.runtime.pending_targets=["tv"]', 'state.runtime.stream=true',
    'state.runtime.pending=true', 'state.runtime.source={kind:"browser"}',
    'state.runtime.phase="preparing"', 'state.runtime.phase="connecting"',
    'state.runtime.phase="playing"', 'state.runtime.phase="stopping"',
    'state.runtime.phase="error"', 'state.diagnostics.active=true',
    'state.recordings.active={id:"capture"}', 'state.recordings.analysis.status="running"',
    'state.setup.complete=false', 'busy=true',
])
def test_frame_stage_start_requires_open_browser_and_fully_idle_media(condition):
    run_recording_ui(condition + ';renderFrameStages();updateButtons();' + """
assert.equal($('measureFrameStages').disabled,true);
assert.deepEqual(calls,[]);
""")


def test_authoritative_media_busy_allows_retired_source_labels_but_blocks_live_stream():
    run_recording_ui("""
state.runtime.phase='error';state.runtime.source={kind:'browser',label:'Previous page'};
state.runtime.media_busy=false;renderFrameStages();updateButtons();
assert.equal($('measureFrameStages').disabled,false);
state.runtime.phase='idle';state.runtime.source=null;state.runtime.media_busy=true;
renderFrameStages();updateButtons();
assert.equal($('measureFrameStages').disabled,true);
assert.match($('frameStagesStatus').textContent,/Stop playback/);
delete state.runtime.media_busy;state.runtime.source={kind:'browser'};
renderFrameStages();assert.equal($('measureFrameStages').disabled,true);
""")


FRAME_STAGE_FIXTURE = r"""
const frameReport={version:1,warmup_seconds:2,duration_seconds:15,requested_fps:30,
 capture:{frames:450,fps:30,nonmonotonic_pts:0,pts:{median_ms:33.3,p95_ms:34,max_ms:100,gaps_over_50ms:1}},
 gate:{accepted:449,rejected:1},converted:{frames:448,fps:29.8},encoded:{frames:447,fps:29.5},
 raw_picture:{frames:450,regions:result.regions.map(region=>({...region,exact_updates:150,exact_updates_per_second:10,longest_exact_hold_ms:100}))},
 timings_ms:{conversion:{count:448,total:224,mean:0.5,p95:1,max:2},upload:{count:448,total:224,mean:0.5,p95:1,max:2},
 encode:{count:448,total:224,mean:0.5,p95:1,max:2},observation:{count:450,total:225,mean:0.5,p95:1,max:2}}};
"""


def test_frame_stage_results_show_raw_gate_encoded_and_picture_limits():
    run_recording_ui(FRAME_STAGE_FIXTURE + r"""
state.frame_probe={status:'complete',error:'',report:frameReport};renderFrameStages();
const markup=$('frameStagesResults').innerHTML;
for(const label of ['Raw capture','Gate accepted','Converted','Encoded output','Pre-encode picture samples','Raw capture PTS gaps','Stage call times','Mean','P95','Max','Picture observation'])assert.match(markup,new RegExp(label));
for(const number of ['30.0 fps','29.9 fps','29.8 fps','29.5 fps','100.0 ms','10.0/s','3.0–6.0/s','0.50'])assert.equal(markup.includes(number),true,number);
assert.match(markup,/not source frame identities/);
assert.match(markup,/do not measure decoded video fps or TV presentation/);
assert.match(markup,/Static scenes, sampling and thresholds/);
assert.match(markup,/accepted converted luma/);
assert.match(markup,/measurement-window edges/);
assert.match(markup,/packet handling, not hardware completion latency/);
assert.deepEqual(calls,[]);
""")


def test_frame_stage_cancel_error_and_running_preserve_last_completed_result():
    run_recording_ui(FRAME_STAGE_FIXTURE + """
state.frame_probe={status:'running',error:'',report:frameReport};renderFrameStages();
assert.match($('frameStagesResults').innerHTML,/Last completed measurement/);
state.frame_probe.status='cancelled';renderFrameStages();
assert.equal($('cancelFrameStages').hidden,true);
assert.equal($('measureFrameStages').disabled,false);
assert.match($('frameStagesStatus').textContent,/cancelled/);
state.frame_probe.status='error';state.frame_probe.error='<img private>';renderFrameStages();
assert.equal($('frameStagesStatus').textContent,'<img private>');
assert.equal($('frameStagesStatus').innerHTML,'');
assert.match($('frameStagesResults').innerHTML,/Last completed measurement/);
""")


def test_frame_stage_rendering_accepts_only_numeric_whitelisted_fields():
    run_recording_ui(FRAME_STAGE_FIXTURE + r"""
frameReport.capture.fps='private.invalid/token';frameReport.gate.accepted=-1;
frameReport.converted.fps=NaN;frameReport.encoded.fps=Infinity;
frameReport.timings_ms.conversion.mean='<img private>';frameReport.timings_ms['private-stage']={mean:999};
frameReport.raw_picture.regions.push({name:'private-region',exact_updates_per_second:999,thresholds:[]});
const markup=frameStagesMarkup(frameReport);
for(const text of ['private','Infinity','NaN','999','<img'])assert.equal(markup.includes(text),false,text);
assert.match(markup,/— fps/);
assert.equal(frameStagesMarkup({...frameReport,version:2}),'');
""")


def test_unmeasured_gpu_upload_stage_is_not_reported_as_exercised():
    run_recording_ui(FRAME_STAGE_FIXTURE + r"""
frameReport.timings_ms.upload={count:0,total:0,mean:0,p95:0,max:0};
assert.match(frameStagesMarkup(frameReport),/<th scope="row">GPU upload<\/th><td>—<\/td><td>—<\/td><td>—<\/td>/);
""")
