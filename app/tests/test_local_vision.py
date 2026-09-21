import base64
import json

import pytest

from app.ai.local_vision import LocalVisionAssistant, PROMPT_VERSION


def valid_response(**overrides):
    value = dict(
        documentType='科研试验记录', summary='阶段试验记录',
        visibleText=['项目编号：青岚-07'], facts=['样本数量为24'],
        chartFindings=['阶段3得分89，为最高值'], uncertainties=[],
    )
    value.update(overrides)
    return {'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':json.dumps(value,ensure_ascii=False)}}],
            'usage':{'prompt_tokens':22,'completion_tokens':11}}


def test_local_vision_sends_bounded_image_and_returns_structured_observation():
    calls=[]
    assistant=LocalVisionAssistant(base_url='http://127.0.0.1:18081',model='Qwen3.5-9B',
        transport=lambda **request:calls.append(request) or valid_response())
    image=b'\x89PNG\r\n\x1a\nexample'
    result=assistant.generate(image_bytes=image,media_type='image/png',filename='记录.png')
    payload=calls[0]['payload']; content=payload['messages'][1]['content']
    assert base64.b64decode(content[1]['image_url']['url'].split(',',1)[1])==image
    assert payload['temperature']==0 and payload['chat_template_kwargs']=={'enable_thinking':False}
    assert payload['response_format']['json_schema']['strict'] is True
    assert result['facts']==['样本数量为24'] and result['model']=='Qwen3.5-9B'
    assert result['promptVersion']==PROMPT_VERSION and result['usage']=={'inputTokens':22,'outputTokens':11}
    assert calls[0]['no_proxy'] is True


@pytest.mark.parametrize('response', [
    {'choices':[]}, valid_response(facts='not-a-list'),
    valid_response(documentType='',summary='',visibleText=[],facts=[],chartFindings=[]),
])
def test_local_vision_rejects_incomplete_or_empty_provider_output(response):
    assistant=LocalVisionAssistant(base_url='http://127.0.0.1:18081',model='local',transport=lambda **kw:response)
    with pytest.raises(RuntimeError):
        assistant.generate(image_bytes=b'image',media_type='image/png',filename='x.png')


def test_local_vision_rejects_unsupported_input_before_transport():
    calls=[]
    assistant=LocalVisionAssistant(base_url='http://127.0.0.1:18081',model='local',transport=lambda **kw:calls.append(kw))
    with pytest.raises(ValueError):
        assistant.generate(image_bytes=b'image',media_type='image/gif',filename='x.gif')
    assert not calls
