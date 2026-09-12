"""Finalize and check unsigned Cherri 2.3.0 output; never sign or add credentials."""

import argparse
import plistlib
from pathlib import Path


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def strings_in(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from strings_in(child)
    elif isinstance(value, list):
        for child in value:
            yield from strings_in(child)


def finalize(path):
    workflow = plistlib.loads(path.read_bytes())
    actions = workflow['WFWorkflowActions']
    # Exported Shortcuts use `text`, with an explicit token-string input.
    # Missing input opens a runtime text prompt; preceding actions are not enough.
    for action in actions:
        if action['WFWorkflowActionIdentifier'] == 'is.workflow.actions.text.match':
            value = action['WFWorkflowActionParameters']['text']
            assert value['WFSerializationType'] == 'WFTextTokenString'
            assert value['Value']['string'] == '\ufffc'
            reference, = value['Value']['attachmentsByRange'].values()
            assert reference['Type'] == 'ActionOutput'
    names = {
        a.get('WFWorkflowActionParameters', {}).get('CustomOutputName'): i
        for i, a in enumerate(actions)
    }
    questions = workflow['WFWorkflowImportQuestions']
    assert len(questions) == 3, 'Expected three setup questions'
    config = ('baseURL', 'clientID', 'clientSecret')
    labels = ('API Base URL', 'Access Client ID', 'Access Client Secret')
    for name, label in zip(config, labels, strict=False):
        question, = [q for q in questions if q['Text'].startswith(label)]
        index = names[name]
        parameters = actions[index]['WFWorkflowActionParameters']
        assert actions[index]['WFWorkflowActionIdentifier'] == 'is.workflow.actions.gettext'
        assert parameters['WFTextActionText'] == ''
        assert question['DefaultValue'] == ''
        assert question['ParameterKey'] == 'WFTextActionText'
        question['ActionIndex'] = index
    questions.sort(key=lambda q: q['ActionIndex'])

    upload = actions[names['created']]
    assert upload['WFWorkflowActionIdentifier'] in (
        'is.workflow.actions.rawaction', 'is.workflow.actions.downloadurl'
    )
    upload['WFWorkflowActionIdentifier'] = 'is.workflow.actions.downloadurl'
    params = upload['WFWorkflowActionParameters']
    assert params['WFHTTPMethod'] == 'POST'
    assert params['WFHTTPBodyType'] == 'File'
    assert params['WFRequestVariable']['Value'] == {
        'Type': 'Variable', 'VariableName': 'inputFile'
    }
    assert not any(
        a['WFWorkflowActionIdentifier'] == 'is.workflow.actions.rawaction' for a in actions
    )

    uuids = {
        a.get('WFWorkflowActionParameters', {}).get('UUID'): i
        for i, a in enumerate(actions)
        if a.get('WFWorkflowActionParameters', {}).get('UUID')
    }
    for index, action in enumerate(actions):
        for node in walk(action):
            if node.get('Type') == 'ActionOutput':
                assert node['OutputUUID'] in uuids, ('Missing output', index, node)
                assert uuids[node['OutputUUID']] < index, ('Forward reference', index, node)

    http = [
        a for a in actions
        if a['WFWorkflowActionIdentifier'] == 'is.workflow.actions.downloadurl'
    ]
    assert len(http) == 15
    for action in http:
        params = action['WFWorkflowActionParameters']
        fields = params['WFHTTPHeaders']['Value']['WFDictionaryFieldValueItems']
        headers = {f['WFKey']['Value']['string']: f['WFValue'] for f in fields}
        for header, name in (
            ('CF-Access-Client-Id', 'clientID'), ('CF-Access-Client-Secret', 'clientSecret')
        ):
            value = headers[header]['Value']
            assert value['string'] == '\ufffc'
            ref, = value['attachmentsByRange'].values()
            expected_uuid = actions[names[name]]['WFWorkflowActionParameters']['UUID']
            assert ref['OutputUUID'] == expected_uuid
        if params['WFHTTPMethod'] == 'POST':
            assert 'Upload-Filename' in headers
        destination = params['WFURL']['Value']
        assert destination['string'] == '\ufffc'
        ref, = destination['attachmentsByRange'].values()
        expected = {'POST': 'createURL', 'DELETE': 'statusURL'}
        if params['WFHTTPMethod'] in expected:
            assert ref['VariableName'] == expected[params['WFHTTPMethod']]
        else:
            assert ref['VariableName'] in ('statusURL', 'contentURL')
    repeats = [a['WFWorkflowActionParameters'] for a in actions
               if a['WFWorkflowActionIdentifier'] == 'is.workflow.actions.repeat.count']
    assert len(repeats) == 2 and repeats[0]['WFRepeatCount'] == 200
    assert actions[-1]['WFWorkflowActionIdentifier'] == 'is.workflow.actions.share'
    assert actions[-2]['WFWorkflowActionParameters']['WFHTTPMethod'] == 'DELETE'
    assert actions[-3]['WFWorkflowActionIdentifier'] == 'is.workflow.actions.setitemname'
    assert actions[-4]['WFWorkflowActionParameters']['CustomOutputName'] == 'downloaded'
    assert actions[-5]['WFWorkflowActionIdentifier'] == 'is.workflow.actions.delay'
    strings = set(strings_in(workflow))
    if path.name.startswith('Convert Media'):
        targets = {
            'video-mp4', 'video-webm', 'image-jpeg', 'image-png', 'image-webp',
            'animation-gif', 'audio-m4a', 'audio-mp3', 'audio-opus',
        }
        assert targets <= strings
        assert any('/v1/jobs?target=' in value for value in strings)
    else:
        assert any('/v1/compressions' in value for value in strings)
        assert not any('/v1/compressions?' in value for value in strings)
    workflow['WFWorkflowName'] = path.name.removesuffix('_unsigned.shortcut')
    path.write_bytes(plistlib.dumps(workflow, sort_keys=False))
    print(
        f'PASS: {path.name}: {len(actions)} actions, '
        '3 blank setup questions, validated HTTP references'
    )


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('files', nargs='+', type=Path)
    args = parser.parse_args()
    for file in args.files:
        finalize(file)
