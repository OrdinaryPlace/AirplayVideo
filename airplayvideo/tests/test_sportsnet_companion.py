"""Fixed Sportsnet DOM intents: no page text or credential values leave Chrome.

These small DOM doubles exercise classification and action guards. The real
Chrome fixture separately qualifies trusted field input; this is not provider
authentication or password-store end-to-end verification.
"""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


SOURCE = (Path(__file__).parents[1] / 'companion/sportsnet.js').read_text()
HARNESS = r'''
const assert = require('node:assert/strict');
let elements=[], focused=true, overlay=null;
global.window={}; window.top=window;
global.innerWidth=1920; global.innerHeight=1080;
global.location={origin:'https://account.sportsnet.ca',pathname:'/signin',href:'https://account.sportsnet.ca/signin'};
global.getComputedStyle=element=>({display:element.hidden?'none':'block',visibility:'visible',opacity:'1'});
function element(kind, text='', options={}){
  const attributes=options.attributes||{};
  return {kind, textContent:text, innerText:text, isConnected:true, parentElement:null,
    disabled:false, hidden:false, value:'', focused:0, clicked:0,
    getAttribute:key=>attributes[key]||null,
    getBoundingClientRect:()=>({left:200,top:300,width:400,height:50,right:600,bottom:350}),
    contains(other){return other===this;},focus(){this.focused++;},click(){this.clicked++;}, ...options};
}
global.document={readyState:'complete',hasFocus:()=>focused,fullscreenElement:null,
  querySelectorAll:selector=>elements.filter(element=>{
    if(selector.startsWith('input[autocomplete="one-time-code"]'))return element.kind==='challenge';
    if(selector.startsWith('[role="dialog"]'))return element.kind==='heading';
    if(selector.startsWith('[role="alert"]'))return element.kind==='error';
    if(selector==='input[type="password"]')return element.kind==='password';
    if(selector.startsWith('input[type="email"]'))return element.kind==='email';
    if(selector.startsWith('button, a[href]'))return element.kind==='button';
    if(selector==='.account-nav--signed-in .account-nav__profile')return element.kind==='account';
    if(selector==='video')return element.kind==='video';
    throw new Error('Unknown selector '+selector);
  }),elementFromPoint:()=>overlay||elements.find(element=>['email','password'].includes(element.kind))};
const status=(extra={})=>sportsnetControl({action:'sportsnet_status',...extra});
const step=(stage,operation='advance',extra={})=>sportsnetControl({action:'sportsnet_step',expected_stage:stage,operation,...extra});
function origin(value,path='/'){location.origin=value;location.pathname=path;location.href=value+path;}
function clean(result){
  const encoded=JSON.stringify(result);
  for(const secret of ['synthetic-email@example.test','synthetic-password','private-page-error','/signin']){
    assert(!encoded.includes(secret),'Sensitive or page-derived content escaped');
  }
}
'''


def run(script):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for Sportsnet companion checks')
    result = subprocess.run([node], input=SOURCE + '\n' + HARNESS + script,
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_email_uses_only_existing_autofill_and_exact_continuation():
    run(r'''
const email=element('email'), next=element('button','Continue');
elements=[email,next];
let state=status(); assert.equal(state.stage,'email');assert.equal(state.filled,false);
assert.equal(state.next,'autofill');assert.deepEqual(state.rect,{x:200,y:300,width:400,height:50,viewport_width:1920,viewport_height:1080});
assert.equal(email.focused,0);assert.equal(next.clicked,0);clean(state);
assert.equal(step('email').acted,false);assert.equal(next.clicked,0);
assert.equal(step('email','field').acted,true);assert.equal(email.focused,1);assert.equal(next.clicked,0);
email.value='synthetic-email@example.test';
state=status();assert.equal(state.filled,true);assert.equal(state.next,'continue');clean(state);
assert.equal(step('password').acted,false);assert.equal(next.clicked,0);
focused=false;assert.equal(step('email').acted,false);focused=true;
next.disabled=true;assert.equal(step('email').acted,false);next.disabled=false;
assert.equal(step('email').acted,true);assert.equal(next.clicked,1);
''')


def test_password_submission_never_returns_values_and_needs_matching_stage():
    run(r'''
const password=element('password','',{value:'synthetic-password'}), signin=element('button','Sign in');
elements=[password,signin];
let state=status();assert.equal(state.stage,'password');assert.equal(state.filled,true);clean(state);
assert.equal(step('email').acted,false);assert.equal(signin.clicked,0);
assert.equal(step('password','field').acted,true);assert.equal(signin.clicked,0);
state=step('password');assert.equal(state.acted,true);assert.equal(signin.clicked,1);clean(state);
password.value='';assert.equal(step('password').acted,false);assert.equal(signin.clicked,1);
elements.push(element('password'));assert.equal(status().stage,'unknown');
''')


def test_challenges_errors_and_external_form_actions_block_credentials():
    run(r'''
const password=element('password','',{value:'synthetic-password'}), signin=element('button','Sign in');
elements=[password,signin,element('challenge')];
assert.equal(status().stage,'verification');assert.equal(step('password').acted,false);assert.equal(signin.clicked,0);
elements=[password,signin,element('heading','Enter your verification code')];assert.equal(status().stage,'verification');
elements=[password,signin,element('heading','Reset your password')];assert.equal(status().stage,'verification');
elements=[password,signin,element('error','Incorrect password private-page-error')];assert.equal(status().stage,'error');clean(status());
elements=[password,signin];password.form=element('form','',{attributes:{action:'https://example.test/collect'}});
assert.equal(status().stage,'unknown');assert.equal(step('password').acted,false);assert.equal(signin.clicked,0);
''')


def test_foreign_origins_subframes_and_obscured_fields_never_allow_native_intent():
    run(r'''
const email=element('email'),next=element('button','Continue');elements=[email,next];
origin('https://account.sportsnet.ca.evil.test');assert.equal(status().stage,'other');
origin('http://account.sportsnet.ca');assert.equal(status().stage,'other');
origin('https://account.sportsnet.ca');window.top={};assert.equal(status().stage,'other');window.top=window;
overlay=element('overlay');assert.equal(status().rect,undefined);assert.equal(step('email','field').acted,false);
overlay=null;email.getBoundingClientRect=()=>({left:-1,top:0,width:400,height:50,right:399,bottom:50});
assert.equal(status().rect,undefined);assert.equal(step('email','field').acted,false);
assert.equal(next.clicked,0);
''')


def test_watch_choices_never_choose_purchase_or_tv_provider():
    run(r'''
origin('https://watch.sportsnet.ca');
const purchase=element('button','Subscribe'),provider=element('button','TV Provider'),register=element('button','Register');
elements=[purchase,provider,register];assert.equal(status().stage,'unknown');assert.equal(step('signin_choice').acted,false);
const entry=element('button','Sign In');elements.push(entry);
assert.equal(status().next,'signin_entry');assert.equal(step('signin_choice').acted,true);assert.equal(entry.clicked,1);
const subscription=element('button','Sportsnet+ Sign in directly with your Sportsnet+ subscription');elements=[purchase,provider,subscription];
assert.equal(status().next,'subscription');assert.equal(step('signin_choice').acted,true);
assert.equal(subscription.clicked,1);assert.equal(provider.clicked,0);assert.equal(purchase.clicked,0);assert.equal(register.clicked,0);
''')


def test_signed_in_and_playing_need_provider_indicators_and_safe_target_identity():
    run(r'''
origin('https://watch.sportsnet.ca');elements=[element('profile')];assert.equal(status().stage,'unknown');
elements=[element('account')];assert.equal(status().stage,'signed_in');
origin('https://watch.sportsnet.ca','/watch/Buffalo_Washington_229550');
const video=element('video','',{readyState:2,ended:false,currentTime:12.5,paused:false,muted:false});elements=[video];
let state=status({target_url:location.href});assert.equal(state.stage,'playing');assert.equal(state.target_matches,true);
assert.equal(state.video_time,12.5);assert.equal(state.paused,false);assert.equal(state.muted,false);
assert.equal(status({target_url:'https://watch.sportsnet.ca/watch/Other_1'}).target_matches,false);
assert.equal(status({target_url:location.href+'?private=1'}).target_matches,false);
origin('https://watch.sportsnet.ca','/channel/Sportsnet_East_155046');
assert.equal(status({target_url:location.href}).target_matches,true);
document.fullscreenElement=video;assert.equal(status().fullscreen,true);
assert.equal(step('playing').acted,false);assert.equal(video.clicked,0);
video.currentTime=Infinity;assert.equal(status().stage,'unknown');assert.equal(status().video_time,null);
''')


def test_background_injects_only_isolated_top_level_scripts_on_exact_origins():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for Sportsnet companion checks')
    source = (Path(__file__).parents[1] / 'companion/background.js').read_text()
    source = source[source.index('async function activeTab()'):source.index('function connect()')]
    harness = r'''
const assert=require('node:assert/strict');
let url='https://watch.sportsnet.ca/watch/Example_123',state='complete',granted=false;
const injected=[];
global.sportsnetControl=()=>{};
global.chrome={tabs:{query:async()=>[{id:7,windowId:11,url,status:state}]},
  permissions:{contains:async options=>{
    assert.deepEqual(options,{origins:['https://watch.sportsnet.ca/*','https://account.sportsnet.ca/*','https://rogers-prd.transmit.security/*']});
    return granted;
  }},
  scripting:{executeScript:async request=>{injected.push(request);return [{result:{stage:'email',filled:false}}];}}};
(async()=>{
  assert.equal((await command({action:'sportsnet_status'})).stage,'permission');
  assert.equal((await command({action:'sportsnet_step',expected_stage:'email',operation:'advance'})).stage,'permission');
  assert.equal(injected.length,0);granted=true;
  let result=await command({action:'sportsnet_status',target_url:url,credential:'must-not-be-forwarded'});
  assert.equal(result.stage,'email');assert.equal(injected.length,1);
  assert.equal(injected[0].world,'ISOLATED');assert.deepEqual(injected[0].target,{tabId:7});
  assert.equal(injected[0].args[0].credential,undefined);
  for(const foreign of ['https://watch.sportsnet.ca.evil.test/','http://watch.sportsnet.ca/','https://www.youtube.com/']){
    url=foreign;assert.equal((await command({action:'sportsnet_status'})).stage,'other');
  }
  assert.equal(injected.length,1);
  url='https://account.sportsnet.ca/signin';state='loading';
  assert.equal((await command({action:'sportsnet_step',expected_stage:'email',operation:'advance'})).stage,'loading');
  assert.equal(injected.length,1);state='complete';
  await assert.rejects(command({action:'sportsnet_step',expected_stage:'unknown',operation:'advance'}));
  await assert.rejects(command({action:'sportsnet_step',expected_stage:'email',operation:'reveal'}));
  assert.equal(injected.length,1);
  await command({action:'sportsnet_step',expected_stage:'email',operation:'field'});
  assert.equal(injected.length,2);assert.equal(injected[1].args[0].operation,'field');
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run([node], input=source + '\n' + harness,
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_new_sportsnet_origins_do_not_expand_required_upgrade_permissions():
    manifest = json.loads((Path(__file__).parents[1] / 'companion/manifest.json').read_text())
    assert manifest['host_permissions'] == ['https://www.youtube.com/*']
    assert manifest['permissions'] == ['nativeMessaging', 'scripting']
    assert manifest['optional_host_permissions'] == ['https://watch.sportsnet.ca/*',
                                                   'https://account.sportsnet.ca/*',
                                                   'https://rogers-prd.transmit.security/*']
    page = (Path(__file__).parents[1] / 'companion/sportsnet-permissions.html').read_text()
    assert '<script src="sportsnet-permissions.js"></script>' in page
    assert '<script>' not in page and 'onclick=' not in page


def test_permission_page_requests_only_exact_origins_from_a_trusted_user_click():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for Sportsnet companion checks')
    source = (Path(__file__).parents[1] / 'companion/sportsnet-permissions.js').read_text()
    harness = r'''
const assert=require('node:assert/strict');
let handler,granted=false,requests=0,previousUrl='https://watch.sportsnet.ca/watch/Example_123';
const updates=[],removed=[];
const enable={disabled:false,addEventListener:(type,callback)=>{assert.equal(type,'click');handler=callback;}};
const message={textContent:''};
global.document={getElementById:id=>id==='enable'?enable:message};
global.chrome={permissions:{request:async options=>{
  requests++;assert.deepEqual(options,{origins:['https://watch.sportsnet.ca/*','https://account.sportsnet.ca/*','https://rogers-prd.transmit.security/*']});return granted;
}},tabs:{getCurrent:async()=>({id:8,openerTabId:7,windowId:11}),
  get:async id=>({id,windowId:11,url:previousUrl}),update:async(...args)=>updates.push(args),remove:async id=>removed.push(id)}};
'''
    script = r'''
(async()=>{
  assert.equal(requests,0);await handler({isTrusted:false});assert.equal(requests,0);
  await handler({isTrusted:true});assert.equal(requests,1);assert.equal(enable.disabled,false);
  assert.deepEqual(updates,[]);assert.deepEqual(removed,[]);
  granted=true;await handler({isTrusted:true});assert.equal(requests,2);
  assert.deepEqual(updates,[[7,{active:true}]]);assert.deepEqual(removed,[8]);
  enable.disabled=false;previousUrl='https://unrelated.example/';await handler({isTrusted:true});
  assert.equal(updates.length,1);assert.equal(removed.length,1);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run([node], input=harness + '\n' + source + '\n' + script,
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_enable_control_opens_permission_page_without_requesting_a_grant():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for Sportsnet companion checks')
    source = (Path(__file__).parents[1] / 'companion/background.js').read_text()
    source = source[source.index('async function activeTab()'):source.index('function connect()')]
    harness = r'''
const assert=require('node:assert/strict');
const created=[],updated=[];let prior=[];
global.chrome={runtime:{getURL:path=>'chrome-extension://fixture/'+path},tabs:{
 query:async options=>options.active?[{id:7,windowId:11}]:prior,
 create:async options=>created.push(options),update:async(...args)=>updated.push(args)},
 permissions:{request:()=>{throw new Error('Background must never request optional access')}}};
(async()=>{
 assert.equal((await command({action:'sportsnet_enable'})).ok,true);
 assert.deepEqual(created,[{windowId:11,openerTabId:7,url:'chrome-extension://fixture/sportsnet-permissions.html',active:true}]);
 prior=[{id:6,url:'https://unrelated.example/'},{id:8,url:'chrome-extension://fixture/sportsnet-permissions.html'}];await command({action:'sportsnet_enable'});
 assert.equal(created.length,1);assert.deepEqual(updated,[[8,{active:true}]]);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run([node], input=source + '\n' + harness,
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
