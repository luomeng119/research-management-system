import pytest

from app.services.proposals import BODY_FIELDS, ProposalServiceError, _validate_body
from app.tests.test_proposals import SOURCE, engine, service


TEXT = '演练：x² 与 x₂，面积10 m²；剂量5 µg，μ不替换。\n保留Ａ１①㎏与\t制表符\u200d连接符'
FIELDS = [field for field in BODY_FIELDS if field != 'sourceType']


@pytest.mark.parametrize('field', FIELDS)
def test_body_save_readback_preserves_prose_characters(service, field):
    proposal = service.create(SOURCE, actor_user_id=7, request_id='r17-fixture-create')
    updated = service.update(proposal['businessId'], {field: TEXT}, expected_version=proposal['version'], actor_user_id=7, request_id='r17-fixture-update')
    assert updated[field] == TEXT
    assert service.get(proposal['businessId'])[field] == TEXT


def test_edge_cleanup_preserves_interior_and_source_enum_normalization():
    payload = {**SOURCE, 'expectedOutcomes': '\x00\u200b\u3000' + TEXT + '\u3000\ufeff\x00', 'sourceType': ' ＩＤＥＡ '}
    values = _validate_body(payload)
    assert values['expected_outcomes'] == TEXT
    assert values['source_type'] == 'IDEA'


@pytest.mark.parametrize('value', ['\x00\u200b', '\ufe0f', None, 123, 'x' * 10001, '内容\x00内容'])
def test_invalid_prose_is_rejected(value):
    with pytest.raises(ProposalServiceError) as error:
        _validate_body({**SOURCE, 'expectedOutcomes': value})
    assert error.value.code == 'VALIDATION_ERROR'
    assert 'expectedOutcomes' in error.value.fields
