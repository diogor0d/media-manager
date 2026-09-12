"""Regression checks against the origin pattern in each generated artifact."""

import plistlib
import re
import unittest
from pathlib import Path


class OriginTests(unittest.TestCase):
    def test_generated_origin_allowlist(self):
        for name in ("Convert Media", "Compress Media"):
            path = Path(__file__).resolve().parents[1] / f"{name}_unsigned.shortcut"
            workflow = plistlib.loads(path.read_bytes())
            actions = workflow["WFWorkflowActions"]
            params, = [a["WFWorkflowActionParameters"] for a in actions
                       if a.get("WFWorkflowActionParameters", {}).get("CustomOutputName")
                       == "validOrigin"]
            pattern = params["WFMatchTextPattern"]
            self.assertEqual(params['text']['WFSerializationType'], 'WFTextTokenString')
            value = params['text']['Value']
            self.assertEqual(value['string'], '\ufffc')
            reference, = value["attachmentsByRange"].values()
            self.assertEqual(reference["OutputName"], "baseURL")
            self.assertEqual(reference["OutputUUID"],
                             actions[0]["WFWorkflowActionParameters"]["UUID"])
            # ICU uses \z; Python's equivalent strict end-of-string anchor is \Z.
            pattern = pattern.replace(r"\z", r"\Z")
            for origin in ("https://converter.example.com", "https://127.0.0.1:8443"):
                with self.subTest(shortcut=name, accepted=origin):
                    self.assertIsNotNone(re.search(pattern, origin))
            for origin in (
                "http://converter.example.com", "https://", "https://example.com/",
                "https://example.com/path", "https://example.com?x=1",
                "https://example.com#fragment", "https://user:pass@example.com",
                "https://example.com@evil.example", "https://example.com\\evil",
                "https://example.com\n", " https://example.com", "https://example.com ",
                "https://example.com%2fevil", "https://example.com:123456",
            ):
                with self.subTest(shortcut=name, rejected=origin):
                    self.assertIsNone(re.search(pattern, origin))
            components = [
                a["WFWorkflowActionParameters"]["WFURLComponent"] for a in actions
                if a["WFWorkflowActionIdentifier"] == "is.workflow.actions.geturlcomponent"
            ]
            self.assertEqual(components, ["Port"])

    def test_upload_diagnostic_uses_actual_post_output(self):
        for name in ("Convert Media", "Compress Media"):
            path = Path(__file__).resolve().parents[1] / f"{name}_unsigned.shortcut"
            actions = plistlib.loads(path.read_bytes())["WFWorkflowActions"]
            index = next(i for i, action in enumerate(actions)
                         if action.get('WFWorkflowActionParameters', {}).get('CustomOutputName')
                         == 'created')
            upload = actions[index]['WFWorkflowActionParameters']
            self.assertEqual(upload['WFHTTPMethod'], 'POST')
            self.assertEqual(actions[index + 1]['WFWorkflowActionIdentifier'],
                             'is.workflow.actions.alert')
            preview = actions[index + 2]
            self.assertEqual(preview['WFWorkflowActionIdentifier'],
                             'is.workflow.actions.previewdocument')
            reference = preview['WFWorkflowActionParameters']['WFInput']['Value']
            self.assertEqual(reference['OutputUUID'], upload['UUID'])
            parser = actions[index + 3]['WFWorkflowActionParameters']
            self.assertEqual(parser['CustomOutputName'], 'creation')
            self.assertEqual(parser['WFInput']['Value']['OutputUUID'], upload['UUID'])

    def test_polling_failure_has_literal_context_before_cleanup(self):
        for name in ("Convert Media", "Compress Media"):
            path = Path(__file__).resolve().parents[1] / f"{name}_unsigned.shortcut"
            actions = plistlib.loads(path.read_bytes())["WFWorkflowActions"]
            index = next(i for i, action in enumerate(actions)
                         if action.get('WFWorkflowActionParameters', {}).get('CustomOutputName')
                         == 'pollingFailure')
            message = actions[index]['WFWorkflowActionParameters']['WFTextActionText']['Value']
            self.assertIn('Polling timed out without a ready result.', message['string'])
            self.assertIn('Completed status requests:', message['string'])
            alert = actions[index + 1]['WFWorkflowActionParameters']
            self.assertEqual(alert['WFAlertActionTitle'], 'Media Manager: Polling Diagnostic v2')
            self.assertEqual(actions[index + 2]['WFWorkflowActionParameters']['WFHTTPMethod'],
                             'DELETE')

    def test_polling_is_gated_only_by_ready_state(self):
        for name in ("Convert Media", "Compress Media"):
            path = Path(__file__).resolve().parents[1] / f"{name}_unsigned.shortcut"
            actions = plistlib.loads(path.read_bytes())["WFWorkflowActions"]
            variables = [a.get('WFWorkflowActionParameters', {}).get('WFVariableName')
                         for a in actions]
            self.assertNotIn('active', variables)
            self.assertNotIn('problem', variables)
            poll = next(i for i, action in enumerate(actions)
                        if action['WFWorkflowActionIdentifier']
                        == 'is.workflow.actions.repeat.count'
                        and action['WFWorkflowActionParameters'].get('WFRepeatCount') == 200)
            guard = actions[poll + 2]['WFWorkflowActionParameters']
            variable = guard['WFInput']['Variable']['Value']
            self.assertEqual(variable['VariableName'], 'ready')
            self.assertEqual(guard['WFCondition'], 4)
            self.assertEqual(guard['WFNumberValue'], 0)
            self.assertEqual(actions[poll + 3]['WFWorkflowActionIdentifier'],
                             'is.workflow.actions.conditional')
            self.assertEqual(actions[poll + 8]['WFWorkflowActionIdentifier'],
                             'is.workflow.actions.downloadurl')

    def test_all_matches_have_explicit_input(self):
        for name in ("Convert Media", "Compress Media"):
            path = Path(__file__).resolve().parents[1] / f"{name}_unsigned.shortcut"
            actions = plistlib.loads(path.read_bytes())["WFWorkflowActions"]
            for action in actions:
                if action['WFWorkflowActionIdentifier'] == 'is.workflow.actions.text.match':
                    params = action['WFWorkflowActionParameters']
                    with self.subTest(shortcut=name, match=params['CustomOutputName']):
                        self.assertEqual(params['text']['Value']['string'], '\ufffc')
                        ref, = params['text']['Value']['attachmentsByRange'].values()
                        self.assertEqual(ref['Type'], 'ActionOutput')

    def test_size_summary_uses_formatted_subtraction_result(self):
        for name in ("Convert Media", "Compress Media"):
            path = Path(__file__).resolve().parents[1] / f"{name}_unsigned.shortcut"
            actions = plistlib.loads(path.read_bytes())["WFWorkflowActions"]
            menu = next(a for a in actions
                        if a['WFWorkflowActionIdentifier'] == 'is.workflow.actions.choosefrommenu'
                        and 'Change:' in str(
                            a['WFWorkflowActionParameters'].get('WFMenuPrompt', '')
                        ))
            prompt = menu['WFWorkflowActionParameters']['WFMenuPrompt']['Value']
            self.assertIn('Input:', prompt['string'])
            self.assertIn('Output:', prompt['string'])
            self.assertIn('positive means smaller', prompt['string'])
            self.assertNotIn(' bytes', prompt['string'])
            formats = [a for a in actions
                       if a['WFWorkflowActionIdentifier'] == 'is.workflow.actions.format.number']
            self.assertEqual(len(formats), 4)
            math_operations = [a['WFWorkflowActionParameters'].get('WFMathOperation')
                               for a in actions
                               if a['WFWorkflowActionIdentifier'] == 'is.workflow.actions.math']
            self.assertIn('-', math_operations)
            self.assertGreaterEqual(math_operations.count('\N{DIVISION SIGN}'), 4)
            self.assertIn('\N{MULTIPLICATION SIGN}', math_operations)

    def test_download_is_materialized_before_cleanup(self):
        for name in ("Convert Media", "Compress Media"):
            path = Path(__file__).resolve().parents[1] / f"{name}_unsigned.shortcut"
            actions = plistlib.loads(path.read_bytes())["WFWorkflowActions"]
            downloaded = next(i for i, action in enumerate(actions)
                              if action.get('WFWorkflowActionParameters', {}).get(
                                  'CustomOutputName') == 'downloaded')
            self.assertEqual(actions[downloaded - 1]['WFWorkflowActionIdentifier'],
                             'is.workflow.actions.delay')
            self.assertEqual(actions[downloaded + 1]['WFWorkflowActionIdentifier'],
                             'is.workflow.actions.setitemname')
            self.assertEqual(actions[downloaded + 2]['WFWorkflowActionParameters']
                             ['WFHTTPMethod'], 'DELETE')
            self.assertEqual(actions[downloaded + 3]['WFWorkflowActionIdentifier'],
                             'is.workflow.actions.share')

    def test_direct_input_offers_photos_then_files(self):
        for name in ("Convert Media", "Compress Media"):
            path = Path(__file__).resolve().parents[1] / f"{name}_unsigned.shortcut"
            actions = plistlib.loads(path.read_bytes())["WFWorkflowActions"]
            menu = next(a for a in actions
                        if a['WFWorkflowActionIdentifier'] == 'is.workflow.actions.choosefrommenu'
                        and a['WFWorkflowActionParameters'].get('WFMenuPrompt')
                        == 'Choose media from')
            items = menu['WFWorkflowActionParameters']['WFMenuItems']
            self.assertEqual([item['WFValue'] for item in items], ['Photos', 'Files'])
            identifiers = [a['WFWorkflowActionIdentifier'] for a in actions]
            self.assertEqual(identifiers.count('is.workflow.actions.selectphoto'), 1)
            self.assertEqual(identifiers.count('is.workflow.actions.file.select'), 1)


if __name__ == "__main__":
    unittest.main()
