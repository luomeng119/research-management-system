import hashlib
import json
import sys

import pytest


from app.tests.ai_eval.dataset import DATASET_SHA256, load_samples
from app.tests.ai_eval.scorer import evaluate
from scripts import run_assistant_eval as runner


def test_records_identify_local_provider():
    record = runner._build_evaluation_record(
        sample_id='x', provider_raw='provider', finalized='final', model='Qwen3.5-9B',
        metadata={'inputTokens': 2, 'outputTokens': 3}, latency_seconds=1, error=None,
    )
    assert record['providerKind'] == 'LOCAL'
    assert record['providerOutputSha256'] == hashlib.sha256(b'provider').hexdigest()
    assert record['finalizedOutputSha256'] == hashlib.sha256(b'final').hexdigest()


def test_scorer_accepts_local_but_requires_independent_review():
    records = [runner._build_evaluation_record(
        sample_id=sample['id'], provider_raw='{}', finalized='{}', model='Qwen3.5-9B',
        metadata={}, latency_seconds=1, error=None,
    ) for sample in load_samples()]
    for record in records:
        record['providerKind'] = 'LOCAL'
    assert evaluate(records)['status'] == 'PENDING_REVIEW'
    records[0]['providerKind'] = 'DEEPSEEK'
    assert evaluate(records)['status'] == 'INVALID_RUN'


def test_runner_no_key_and_checkpoints_completed_samples(monkeypatch, tmp_path):
    from app.ai import local_model
    from app.ai import contract

    output = tmp_path / 'eval.json'
    calls = []

    class Provider:
        last_metadata = {}

        def __init__(self, **kwargs):
            assert kwargs == {'base_url': 'http://127.0.0.1:18081', 'model': 'Qwen3.5-9B'}

        def generate(self, source, **kwargs):
            assert kwargs['deadline_seconds'] == 60
            if calls:
                checkpoint = json.loads(output.read_text())
                assert checkpoint['runStatus'] == 'RUNNING'
                assert len(checkpoint['records']) == len(calls)
            calls.append(source)
            if len(calls) == 2:
                raise RuntimeError('provider unavailable')
            self.last_metadata = {'inputTokens': 4, 'outputTokens': 5}
            return '{}'

    monkeypatch.setattr(local_model, 'LocalProposalAssistant', Provider)
    def finalize(source, raw):
        if source == load_samples()[2]['input']:
            raise ValueError('invalid schema')
        return {'text': source}

    monkeypatch.setattr(contract, 'finalize_assistant_content', finalize)
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    monkeypatch.setenv('DEEPSEEK_MODEL', 'must-not-be-used')
    monkeypatch.setattr(sys, 'argv', ['runner', '--output', str(output)])
    assert runner.main() == 0
    result = json.loads(output.read_text())
    assert len(calls) == len(result['records']) == 20
    assert result['datasetSha256'] == DATASET_SHA256
    assert result['runStatus'] == 'COMPLETE'
    assert result['report']['status'] == 'PENDING_REVIEW'
    assert result['records'][1]['error']['type'] == 'RuntimeError'
    assert result['records'][1]['providerRawOutputAvailable'] is False

    assert result['records'][2]['error']['type'] == 'ValueError'
    assert result['records'][2]['providerRawOutputAvailable'] is True
    assert result['records'][2]['providerRawOutput'] == '{}'
    assert result['records'][2]['providerOutputSha256'] == hashlib.sha256(b'{}').hexdigest()


def test_runner_preserves_existing_evidence(monkeypatch, tmp_path):
    output = tmp_path / 'eval.json'
    output.write_text('prior evidence')
    monkeypatch.setattr(sys, 'argv', ['runner', '--output', str(output)])
    with pytest.raises(SystemExit) as failure:
        runner.main()
    assert failure.value.code == 2
    assert output.read_text() == 'prior evidence'


def test_runner_rejects_modified_frozen_samples(monkeypatch, tmp_path):
    from app.tests.ai_eval import dataset

    samples = load_samples()
    samples[0]['input'] += 'changed'
    monkeypatch.setattr(dataset, 'load_samples', lambda: samples)
    output = tmp_path / 'eval.json'
    monkeypatch.setattr(sys, 'argv', ['runner', '--output', str(output)])
    with pytest.raises(SystemExit) as failure:
        runner.main()
    assert failure.value.code == 2
    assert not output.exists()
