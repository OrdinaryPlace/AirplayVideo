import copy
import json
import pytest
from service.model import Store, UserError, default_setup, atomic_json, validate_generated


def test_additive_upgrade_preserves_saved_identity_and_configuration(tmp_path):
    store = Store(tmp_path)
    store.setup(default_setup())
    store.data['setup'].pop('generated')
    store.data['setup']['modes'].pop('generated')
    store.data['setup']['video']['fps'] = 60
    store.data['receivers'] = [{'id': 'a'*32, 'name': 'Existing TV', 'slot': 3}]
    before = copy.deepcopy(store.data)
    atomic_json(store.path, store.data)
    upgraded = Store(tmp_path).data
    assert upgraded['setup']['generated']['duration_seconds'] == 60
    assert upgraded['setup']['modes']['generated'] is True
    restored = copy.deepcopy(upgraded)
    restored['setup'].pop('generated')
    restored['setup']['modes'].pop('generated')
    assert restored == before
    assert Store(tmp_path).data == upgraded


@pytest.mark.parametrize('value', [None, [], {'title':''}, {'title':'a'*161}, {'tagline':'a'*241},
    {'title':'x\x00y'}, {'title':'\ud800'}, {'duration_seconds':0}, {'duration_seconds':86401},
    {'duration_seconds':True}, {'duration_seconds':1.5}, {'display':[]}, {'display':'unknown'},
    {'timezone':'/etc/passwd'}, {'timezone':'../UTC'}, {'timezone':'Unknown/Zone'}, {'unknown':1}])
def test_invalid_generated_settings(value):
    with pytest.raises(UserError):
        validate_generated(value)


def test_plain_unicode_text_and_partial_automation_overrides():
    defaults = validate_generated({'title':'Saved', 'duration_seconds':90})
    result = validate_generated({'title':'Café "hello" <b>text</b>', 'tagline':'line one\nline two'}, defaults)
    assert result['duration_seconds'] == 90
    assert result['title'] == 'Café "hello" <b>text</b>'
    assert defaults['title'] == 'Saved'
    assert json.loads(json.dumps(result)) == result
