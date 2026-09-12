"""Create ignored, configured test copies without changing distributable masters."""

import json
import plistlib
from pathlib import Path

root = Path(__file__).resolve().parent
private = root / 'private'
config = json.loads((private / 'config.json').read_text(encoding='utf-8'))
diagnostic_name = 'Private Test v5'
for name in ('Convert Media', 'Compress Media'):
    workflow = plistlib.loads((root / f'{name}_unsigned.shortcut').read_bytes())
    for question in workflow['WFWorkflowImportQuestions']:
        action = workflow['WFWorkflowActions'][question['ActionIndex']]
        parameters = action['WFWorkflowActionParameters']
        key = parameters['CustomOutputName']
        assert key in ('baseURL', 'clientID', 'clientSecret')
        assert parameters[question['ParameterKey']] == ''
        parameters[question['ParameterKey']] = config[key]
    workflow['WFWorkflowImportQuestions'] = []
    workflow['WFWorkflowName'] = f'{name} {diagnostic_name}'
    destination = private / f'{name} {diagnostic_name}_unsigned.shortcut'
    destination.write_bytes(plistlib.dumps(workflow, sort_keys=False))
    checked = plistlib.loads(destination.read_bytes())
    assert checked['WFWorkflowImportQuestions'] == []
    populated = {
        a['WFWorkflowActionParameters']['CustomOutputName']:
        a['WFWorkflowActionParameters']['WFTextActionText']
        for a in checked['WFWorkflowActions']
        if a.get('WFWorkflowActionParameters', {}).get('CustomOutputName') in config
    }
    assert populated == config
    master = plistlib.loads((root / f'{name}_unsigned.shortcut').read_bytes())
    assert all(
        master['WFWorkflowActions'][q['ActionIndex']]['WFWorkflowActionParameters']
        [q['ParameterKey']] == '' for q in master['WFWorkflowImportQuestions']
    )
    print(f'Created ignored configured artifact: {destination.name}')
