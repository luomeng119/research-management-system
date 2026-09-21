import re
import subprocess
from pathlib import Path

from app.tests.test_generic_tables_req020 import _client


def test_current_and_snapshot_labels_follow_table_current_id(tmp_path):
    client = _client(tmp_path)
    table_id = client.post('/api/generic-tables', json={'name': '标签测试'}).get_json()['table_id']
    first = client.get(f'/api/generic-tables/{table_id}/versions').get_json()['versions'][0]['version_id']
    saved = client.post(f'/api/generic-tables/{table_id}/versions', json={
        'method': 'snapshot', 'source_version_id': first, 'label': '历史标签',
    }).get_json()
    current = saved['new_current_id']
    histories = {first, saved['snapshot_id']}
    for viewed in [current, *histories]:
        response = client.get(f'/tables/{table_id}/version/{viewed}/')
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        select = html.split('id="versionSelect"', 1)[1].split('</select>', 1)[0]
        options = re.findall(r'<option value="([^"]*)"([^>]*)>(.*?)</option>', select, re.S)
        assert len(options) == 3
        assert {value for value, _, _ in options if value} == histories
        selected = [value for value, attrs, _ in options if 'selected' in attrs]
        assert selected == ([''] if viewed == current else [viewed])
        aside = html.split('<aside class="version-sidebar"', 1)[1].split('</aside>', 1)[0]
        assert set(re.findall(r'href="/tables/[^/]+/version/([^"]+)"', aside)) == histories
        assert ('正在查看的历史快照' in aside) == (viewed != current)


def test_snapshot_success_does_not_invent_current_name_from_id():
    source = (Path(__file__).parents[1] / 'templates/generic_tables/detail.html').read_text()
    function = source.split('async function doSaveSnapshot(', 1)[1].split('// ----------', 1)[0]
    script = '''const assert=require('node:assert/strict');
const CAN_EDIT=true,VERSION_ID='source';let message='',reloaded=false;
const fetch=async()=>({json:async()=>({new_current_id:'GTVrandomABC123',snapshot_id:'snapshot'})});
const alert=value=>{message=value;};const location={reload(){reloaded=true;}};
''' + 'async function doSaveSnapshot(' + function + '''
(async()=>{await doSaveSnapshot('输入的历史标签','');assert.ok(message.includes('输入的历史标签'));assert.ok(!message.includes('ABC123'));assert.ok(message.includes('当前编辑版本'));assert.equal(reloaded,true);})();
'''
    subprocess.run(['node', '-e', script], check=True)
