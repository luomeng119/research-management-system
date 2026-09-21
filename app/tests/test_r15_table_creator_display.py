from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from app.tests.test_generic_tables_req020 import _client


@pytest.mark.parametrize('known', [True, False])
def test_current_and_snapshot_creator_name_read_only(tmp_path, known):
    client = _client(tmp_path)
    with client.session_transaction() as session:
        session['user'] = 'fixture_owner'
    table_id = client.post('/api/generic-tables', json={'name': '演练姓名显示'}).get_json()['table_id']
    versions_url = f'/api/generic-tables/{table_id}/versions'
    current = client.get(versions_url).get_json()['versions'][0]['version_id']
    saved = client.post(versions_url, json={'method': 'snapshot', 'source_version_id': current}).get_json()
    before = client.get(versions_url).get_json()
    list_before = client.get('/tables').data
    engine = sa.create_engine('sqlite://')
    users = sa.Table('users', sa.MetaData(), sa.Column('username', sa.String, primary_key=True),
                     sa.Column('name', sa.String), sa.Column('password', sa.String))
    users.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(users.insert(), [{'username': 'fixture_owner' if known else 'unrelated',
                                             'name': '李老师', 'password': 'never-read'}])
    client.application.extensions['users_repository'] = SimpleNamespace(engine=engine, table=users)
    queries = []
    sa.event.listen(engine, 'before_cursor_execute', lambda conn, cursor, statement, params, ctx, many:
                    queries.append((statement, params)))
    for url in [f'/tables/{table_id}', f"/tables/{table_id}/version/{saved['snapshot_id']}/"]:
        queries.clear()
        response = client.get(url)
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        expected = '李老师' if known else 'fixture_owner'
        assert f'title="fixture_owner">{expected}</span>' in html
        assert len(queries) == 1
        assert 'WHERE' in queries[0][0] and ' IN ' in queries[0][0]
        assert 'password' not in queries[0][0]
        assert queries[0][1] == ('fixture_owner',)
    assert client.get(versions_url).get_json() == before
    assert client.get('/tables').data == list_before
