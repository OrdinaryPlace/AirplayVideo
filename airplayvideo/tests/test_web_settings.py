"""Sportsnet rendering uses the existing isolated settings save/draft boundary."""
from pathlib import Path
import re
import shutil
import subprocess

import pytest


SOURCE = (Path(__file__).resolve().parents[1] / 'web' / 'app.js').read_text()


def run_settings(script):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for browser settings checks')
    functions = []
    for name in ('collectStep', 'pageChanges', 'settingsStatus', 'sportsnetGraphicsFields', 'setupTask'):
        match = re.search(rf'^(?:async )?function {name}\([^\n]*\)\s*\{{.*?^\}}', SOURCE, re.M | re.S)
        assert match, name
        functions.append(match.group())
    functions.append(re.search(r'^function selected\([^\n]+', SOURCE, re.M).group())
    submit = SOURCE[SOURCE.index("$('setupForm').onsubmit = async event => {"):
                    SOURCE.index("$('setupForm').addEventListener('input'")]
    events = '\n'.join(re.search(rf"^\$\('setupForm'\)\.addEventListener\('{kind}'[^\n]+", SOURCE, re.M).group()
                       for kind in ('input', 'change'))
    reset = re.search(r"^\$\('discardSettings'\)\.onclick=[^\n]+", SOURCE, re.M).group()
    harness = r"""
const assert=require('node:assert/strict');
const nodes=new Map();
function node(value=''){return {value,disabled:false,handlers:{},addEventListener(type,handler){this.handlers[type]=handler;}};}
const $=id=>nodes.get(id)||null;
const initial={complete:true,modes:{browser:true,hdhomerun:false,generated:false},
 browser:{home_url:'https://www.youtube.com/',youtube_quality:'1080p',http_proxy:'http://gateway:8888',http_proxy_enabled:true,vpn_name:'Browser VPN',sportsnet_rendering:'vulkan'},
 hdhomerun:{devices:[]},generated:{title:'Keep'},video:{fps:30},audio:{enabled:true},home_assistant:{enabled:true}};
let state={setup:structuredClone(initial),runtime:{phase:'idle',targets:[],media_busy:false}},draft=structuredClone(initial),setupBase=structuredClone(initial),step=1,settingsSaving=false;
const stepSections=[['modes'],['browser','hdhomerun','generated'],[],['video','audio'],['home_assistant']];
const controls={homeUrl:'home_url',youtubeQuality:'youtube_quality',browserProxy:'http_proxy',vpnName:'vpn_name',sportsnetRendering:'sportsnet_rendering'};
for(const [id,key]of Object.entries(controls))nodes.set(id,node(draft.browser[key]));
for(const id of ['setupForm','discardSettings','next','settingsStatus'])nodes.set(id,node());
const document={querySelectorAll(){return [];}};
let calls=[],failure='',response=null,notice='',error='';
async function api(path,body){calls.push({path,body:structuredClone(body)});if(failure)throw Error(failure);return structuredClone(response);}
function message(value='',text=''){error=value;notice=text;}
function renderStep(){for(const [id,key]of Object.entries(controls))$(id).value=draft.browser[key]||'';}
function readGenerated(){throw Error('Unrelated defaults must not be collected');}
const event={preventDefault(){}};
"""
    result = subprocess.run([node], input=harness + '\n'.join(functions) + submit + events + reset +
                            '\n(async()=>{' + script + '})().catch(error=>{console.error(error);process.exitCode=1;});',
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_graphics_choice_is_collapsed_and_scoped_to_source_defaults():
    run_settings(r"""
let markup=sportsnetGraphicsFields();
assert.match(markup,/<details id="sportsnetGraphics">/);
assert.equal(/<details[^>]*\bopen\b/.test(markup),false);
assert.match(markup,/<summary>Sportsnet graphics<\/summary>/);
assert.match(markup,/<option value="vulkan" selected>/);
assert.deepEqual([...markup.matchAll(/<option value="([^"]+)"/g)].map(match=>match[1]),['vulkan','opengl']);
assert.match(markup,/sign-in stays saved/);
assert.match(markup,/Saving a change closes Sportsnet if it is open/);
assert.match(markup,/Results depend on the stream and hardware/);
draft.browser.sportsnet_rendering='opengl';markup=sportsnetGraphicsFields();
assert.match(markup,/<option value="opengl" selected>/);
assert.equal(markup.includes('act('),false);
""")
    configure = SOURCE[SOURCE.index('} else if (step === 1) {', SOURCE.index('function renderStep()')):
                       SOURCE.index('} else if (step === 2) {', SOURCE.index('function renderStep()'))]
    assert '${sportsnetGraphicsFields()}' in configure


def test_rendering_input_is_a_draft_until_explicit_page_save():
    run_settings("""
$('sportsnetRendering').value='opengl';$('setupForm').handlers.change();
assert.equal(draft.browser.sportsnet_rendering,'opengl');
assert.equal(state.setup.browser.sportsnet_rendering,'vulkan');
assert.equal(setupBase.browser.sportsnet_rendering,'vulkan');
assert.deepEqual(calls,[]);
step=3;renderStep();step=1;renderStep();
assert.equal($('sportsnetRendering').value,'opengl');
assert.deepEqual(pageChanges(),{changes:{browser:{sportsnet_rendering:'opengl'}},expected:{browser:{sportsnet_rendering:'vulkan'}}});
""")


def test_successful_save_persists_only_rendering_and_preserves_other_page_drafts():
    run_settings("""
$('sportsnetRendering').value='opengl';$('setupForm').handlers.input();
draft.video.fps=60;
response=structuredClone(state);response.setup.browser.sportsnet_rendering='opengl';
response.setup.browser.vpn_name='Concurrent name';
await $('setupForm').onsubmit(event);
assert.deepEqual(calls,[{path:'api/setup/update',body:{changes:{browser:{sportsnet_rendering:'opengl'}},expected:{browser:{sportsnet_rendering:'vulkan'}}}}]);
assert.equal(state.setup.browser.sportsnet_rendering,'opengl');
assert.equal(draft.browser.sportsnet_rendering,'opengl');
assert.equal(setupBase.browser.sportsnet_rendering,'opengl');
assert.equal(draft.browser.vpn_name,'Concurrent name');
assert.equal(state.setup.video.fps,30);assert.equal(draft.video.fps,60);
assert.deepEqual(pageChanges(3),{changes:{video:{fps:60}},expected:{video:{fps:30}}});
assert.deepEqual(pageChanges(1),{changes:{},expected:{}});
assert.match(notice,/Changes on this page saved/);
assert.equal(settingsSaving,false);
""")


def test_reset_reverts_rendering_to_latest_saved_value_without_saving_other_pages():
    run_settings("""
$('sportsnetRendering').value='opengl';collectStep(false);draft.video.fps=60;
$('discardSettings').onclick();
assert.equal(draft.browser.sportsnet_rendering,'vulkan');
assert.equal($('sportsnetRendering').value,'vulkan');
assert.equal(draft.video.fps,60);assert.deepEqual(calls,[]);
state.setup.browser.sportsnet_rendering='opengl';
$('discardSettings').onclick();
assert.equal(draft.browser.sportsnet_rendering,'opengl');
assert.equal(setupBase.browser.sportsnet_rendering,'opengl');
assert.deepEqual(pageChanges(),{changes:{},expected:{}});
""")


def test_conflict_preserves_unsaved_choice_and_original_expected_value():
    run_settings("""
$('sportsnetRendering').value='opengl';$('setupForm').handlers.change();
failure='Settings changed in another window; reset this page and try again';
await $('setupForm').onsubmit(event);
assert.equal(error,failure);
assert.equal(draft.browser.sportsnet_rendering,'opengl');
assert.equal($('sportsnetRendering').value,'opengl');
assert.equal(setupBase.browser.sportsnet_rendering,'vulkan');
assert.deepEqual(calls[0].body.expected,{browser:{sportsnet_rendering:'vulkan'}});
assert.equal(state.setup.browser.sportsnet_rendering,'vulkan');
assert.equal(settingsSaving,false);
""")


@pytest.mark.parametrize('condition', [
    'state.runtime.media_busy=true', 'state.diagnostics={active:true}',
    'state.recordings={active:{id:"sample"}}',
    'state.recordings={analysis:{status:"running"}}',
    'state.frame_probe={status:"running"}',
])
def test_rendering_save_waits_for_actual_media_and_exclusive_measurements(condition):
    run_settings("$('sportsnetRendering').value='opengl';collectStep(false);" + condition + ";settingsStatus();" + """
assert.equal($('next').disabled,true);
assert.match($('settingsStatus').textContent,/before saving settings/);
assert.deepEqual(calls,[]);
""")


def test_stale_error_labels_do_not_block_an_idle_rendering_save():
    run_settings("""
$('sportsnetRendering').value='opengl';collectStep(false);
state.runtime.phase='error';state.runtime.source={kind:'browser'};
settingsStatus();assert.equal($('next').disabled,false);
state.runtime.media_busy=true;settingsStatus();assert.equal($('next').disabled,true);
delete state.runtime.media_busy;state.runtime.phase='stopping';
settingsStatus();assert.equal($('next').disabled,true);
""")
