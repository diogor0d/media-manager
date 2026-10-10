"""Regression checks against the origin pattern in each generated artifact."""

import plistlib
import re
import unittest
from pathlib import Path


class OriginTests(unittest.TestCase):
    def test_estimate_menu_includes_size_change_and_confidence(self):
        source = (Path(__file__).resolve().parents[1] / "job.cherri").read_text()
        self.assertIn("change {reductionMin}% to {reductionMax}%", source)
        self.assertIn("{confidence} confidence", source)
        self.assertIn("change {refinedReductionMin}% to {refinedReductionMax}%", source)
        self.assertIn("{refinedConfidence} confidence", source)

    def test_filename_reads_selected_file_with_native_input_key(self):
        for name in ("Convert Media", "Compress Media"):
            path = Path(__file__).resolve().parents[1] / f"{name}_unsigned.shortcut"
            actions = plistlib.loads(path.read_bytes())["WFWorkflowActions"]
            action, = [a for a in actions
                       if a.get("WFWorkflowActionParameters", {}).get("CustomOutputName")
                       == "inputName"]
            self.assertEqual(action['WFWorkflowActionIdentifier'],
                             'is.workflow.actions.getitemname')
            params = action['WFWorkflowActionParameters']
            self.assertNotIn('WFFolder', params)
            self.assertEqual(params['WFInput']['Value'],
                             {'Type': 'Variable', 'VariableName': 'inputFile'})

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
            base_url = next(a for a in actions
                            if a.get("WFWorkflowActionParameters", {}).get("CustomOutputName")
                            == "baseURL")
            self.assertEqual(reference["OutputUUID"],
                             base_url["WFWorkflowActionParameters"]["UUID"])
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

    def test_upload_parser_uses_actual_post_output_without_preview(self):
        for name in ("Convert Media", "Compress Media"):
            path = Path(__file__).resolve().parents[1] / f"{name}_unsigned.shortcut"
            actions = plistlib.loads(path.read_bytes())["WFWorkflowActions"]
            index = next(i for i, action in enumerate(actions)
                         if action.get('WFWorkflowActionParameters', {}).get('CustomOutputName')
                         == 'created')
            upload = actions[index]['WFWorkflowActionParameters']
            self.assertEqual(upload['WFHTTPMethod'], 'POST')
            self.assertFalse(any(
                a['WFWorkflowActionIdentifier'] == 'is.workflow.actions.previewdocument'
                for a in actions))
            parser = next(a['WFWorkflowActionParameters'] for a in actions[index + 1:]
                          if a.get('WFWorkflowActionParameters', {}).get('CustomOutputName')
                          == 'creation')
            self.assertEqual(parser['CustomOutputName'], 'creation')
            self.assertEqual(parser['WFInput']['Value']['OutputUUID'], upload['UUID'])

    def test_polling_timeout_does_not_delete_automatically(self):
        for name in ("Convert Media", "Compress Media"):
            path = Path(__file__).resolve().parents[1] / f"{name}_unsigned.shortcut"
            actions = plistlib.loads(path.read_bytes())["WFWorkflowActions"]
            index = next(i for i, action in enumerate(actions)
                         if action['WFWorkflowActionIdentifier'] == 'is.workflow.actions.alert'
                         and 'Conversion timed out.' in str(action['WFWorkflowActionParameters']))
            self.assertFalse(any(
                action['WFWorkflowActionIdentifier'] == 'is.workflow.actions.downloadurl'
                and action['WFWorkflowActionParameters'].get('WFHTTPMethod') == 'DELETE'
                for action in actions[max(0, index - 3):index]
            ))
            alert = actions[index]['WFWorkflowActionParameters']
            self.assertEqual(alert['WFAlertActionTitle'], 'Media Manager')
            message = str(alert.get('WFAlertActionMessage', ''))
            self.assertIn('Conversion timed out.', message)
            self.assertLess(len(message), 120)

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
            self.assertGreaterEqual(len(formats), 4)
            math_operations = [a['WFWorkflowActionParameters'].get('WFMathOperation')
                               for a in actions
                               if a['WFWorkflowActionIdentifier'] == 'is.workflow.actions.math']
            self.assertIn('-', math_operations)
            self.assertGreaterEqual(math_operations.count('\N{DIVISION SIGN}'), 4)
            self.assertIn('\N{MULTIPLICATION SIGN}', math_operations)

    def test_download_is_materialized_without_automatic_cleanup(self):
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
            self.assertEqual(actions[downloaded + 2]['WFWorkflowActionIdentifier'],
                             'is.workflow.actions.share')
            self.assertFalse(any(
                action['WFWorkflowActionIdentifier'] == 'is.workflow.actions.downloadurl'
                and action['WFWorkflowActionParameters'].get('WFHTTPMethod') == 'DELETE'
                for action in actions[downloaded + 1:downloaded + 3]
            ))

    def test_native_flow_estimates_then_requires_explicit_start(self):
        for name in ("Convert Media", "Compress Media"):
            path = Path(__file__).resolve().parents[1] / f"{name}_unsigned.shortcut"
            actions = plistlib.loads(path.read_bytes())["WFWorkflowActions"]
            http = [a["WFWorkflowActionParameters"] for a in actions
                    if a["WFWorkflowActionIdentifier"] == "is.workflow.actions.downloadurl"]
            json_posts = [p for p in http if p.get("WFHTTPMethod") == "POST" and p.get("WFHTTPBodyType") == "JSON"]
            self.assertEqual(len(json_posts), 2)
            estimate, start = json_posts
            self.assertEqual(estimate["WFHTTPMethod"], "POST")
            self.assertEqual(estimate["WFHTTPBodyType"], "JSON")
            self.assertEqual(start["WFHTTPMethod"], "POST")
            self.assertEqual(start["WFHTTPBodyType"], "JSON")
            self.assertEqual(estimate["WFURL"]["Value"]["attachmentsByRange"]["{0, 1}"]["VariableName"], "estimateURL")
            self.assertEqual(start["WFURL"]["Value"]["attachmentsByRange"]["{0, 1}"]["VariableName"], "startURL")
            self.assertEqual(start["WFURL"]["Value"]["string"], "\ufffc")
            menus = [str(a.get("WFWorkflowActionParameters", {}).get("WFMenuPrompt", ""))
                     for a in actions if a["WFWorkflowActionIdentifier"] == "is.workflow.actions.choosefrommenu"]
            self.assertTrue(any("Estimate:" in prompt for prompt in menus))
            self.assertTrue(any("Refined estimate:" in prompt for prompt in menus))

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

    def test_input_selection_precedes_configuration_and_setup_questions(self):
        for name in ("Convert Media", "Compress Media"):
            path = Path(__file__).resolve().parents[1] / f"{name}_unsigned.shortcut"
            workflow = plistlib.loads(path.read_bytes())
            actions = workflow["WFWorkflowActions"]
            output_names = [a.get("WFWorkflowActionParameters", {}).get("CustomOutputName")
                            for a in actions]
            count_index = output_names.index("providedInputCount")
            source_count_index = output_names.index("inputCount")
            base_index = output_names.index("baseURL")
            input_assignments = [(i, a) for i, a in enumerate(actions)
                                 if a.get("WFWorkflowActionParameters", {}).get("WFVariableName")
                                 == "inputFile"]
            input_index, input_action = input_assignments[0]
            self.assertLess(count_index, input_index)
            self.assertLess(input_index, source_count_index)
            self.assertLess(source_count_index, base_index)

            provided_count = actions[count_index]
            self.assertEqual(provided_count["WFWorkflowActionIdentifier"],
                             "is.workflow.actions.count")
            self.assertEqual(provided_count["WFWorkflowActionParameters"]["Input"]["Value"],
                             {"Type": "ExtensionInput"})
            self.assertEqual(input_action["WFWorkflowActionIdentifier"],
                             "is.workflow.actions.setvariable")
            self.assertEqual(input_action["WFWorkflowActionParameters"]["WFInput"]["Value"],
                             {"Type": "ExtensionInput"})
            self.assertEqual(input_action["WFWorkflowActionParameters"]["WFVariableName"],
                             "inputFile")
            zero_input_gate = actions[count_index + 2]
            self.assertEqual(zero_input_gate["WFWorkflowActionIdentifier"],
                             "is.workflow.actions.conditional")
            self.assertEqual(zero_input_gate["WFWorkflowActionParameters"]["WFNumberValue"], 0)
            self.assertEqual(actions[count_index + 3]["WFWorkflowActionIdentifier"],
                             "is.workflow.actions.choosefrommenu")
            self.assertEqual(actions[count_index + 3]["WFWorkflowActionParameters"]["WFMenuPrompt"],
                             "Choose media from")
            self.assertEqual(actions[source_count_index]["WFWorkflowActionParameters"]["Input"]["Value"],
                             {"Type": "Variable", "VariableName": "inputFile"})
            exact_one_gate = actions[source_count_index + 1]
            self.assertEqual(exact_one_gate["WFWorkflowActionIdentifier"],
                             "is.workflow.actions.conditional")
            self.assertEqual(exact_one_gate["WFWorkflowActionParameters"]["WFNumberValue"], 1)
            self.assertEqual(actions[source_count_index + 2]["WFWorkflowActionIdentifier"],
                             "is.workflow.actions.alert")

            questions = workflow["WFWorkflowImportQuestions"]
            self.assertEqual(len(questions), 3)
            config = {"baseURL": "API Base URL", "clientID": "Access Client ID",
                      "clientSecret": "Access Client Secret"}
            for variable, label in config.items():
                question, = [q for q in questions if q["Text"].startswith(label)]
                self.assertEqual(question["DefaultValue"], "")
                action_index = question["ActionIndex"]
                self.assertGreater(action_index, source_count_index)
                self.assertEqual(output_names[action_index], variable)
                self.assertEqual(actions[action_index]["WFWorkflowActionIdentifier"],
                                 "is.workflow.actions.gettext")
                self.assertEqual(actions[action_index]["WFWorkflowActionParameters"]
                                 ["WFTextActionText"], "")

    def test_masters_contain_no_runtime_debug_output(self):
        forbidden = ("diagnostic", "debug", "completed status requests", "last state", "attempts:")
        debug_variables = {"idCheck", "stateCheck", "errorCheck", "completedPolls", "lastState"}
        for name in ("Convert Media", "Compress Media"):
            path = Path(__file__).resolve().parents[1] / f"{name}_unsigned.shortcut"
            actions = plistlib.loads(path.read_bytes())["WFWorkflowActions"]
            visible = []
            for action in actions:
                identifier = action["WFWorkflowActionIdentifier"]
                params = action.get("WFWorkflowActionParameters", {})
                self.assertNotEqual(identifier, "is.workflow.actions.previewdocument")
                self.assertNotIn(params.get("WFVariableName"), debug_variables)
                for key in ("WFAlertActionMessage", "WFAlertActionTitle", "WFMenuPrompt"):
                    value = params.get(key)
                    if isinstance(value, str):
                        visible.append(value.lower())
                    elif isinstance(value, dict):
                        visible.append(str(value.get("Value", {})).lower())
            visible_text = "\n".join(visible)
            for marker in forbidden:
                with self.subTest(shortcut=name, marker=marker):
                    self.assertNotIn(marker, visible_text)
            if name == "Compress Media":
                names = [a.get("WFWorkflowActionParameters", {}).get("CustomOutputName")
                         for a in actions]
                self.assertIn("validAttempts", names)
                self.assertIn("attempts", names)


if __name__ == "__main__":
    unittest.main()
