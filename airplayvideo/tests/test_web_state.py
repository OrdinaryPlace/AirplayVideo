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
    for name in ('message', 'renderRuntimeError', 'stableMarkup', 'browserKindChanged', 'renderUsage'):
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
state.runtime.native={phase:'failed', error:'Native video failed', preparation_stage:'verify', failure_stage:'verify'};
state.runtime.source={kind:'browser', label:'YouTube video'};
renderUsage();
assert.equal($('nativePlaybackReport').hidden, false);
assert.match($('nativePlaybackStatus').textContent, /^Previous direct playback/);
assert.match($('nativePlaybackStatus').textContent, /Native video failed/);
// Retaining the failed report must not manufacture a current error banner.
assert.equal($('error').textContent, '');
state.runtime.source={kind:'youtube', label:'YouTube video'};
state.runtime.native.phase='playing';
state.runtime.native.error='';
renderUsage();
assert.equal($('nativePlaybackStatus').textContent, 'Sending direct video');
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
