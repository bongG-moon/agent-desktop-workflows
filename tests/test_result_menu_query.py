"""The UIA query in a physical result-menu gesture can run in the background."""
import unittest
from unittest.mock import patch

import test_python_context_workflow as fixture
from test_python_context_workflow import flow


class ResultQueryTests(unittest.TestCase):
    def setUp(self):
        self.ui = fixture.CompositionTests()
        self.ui.setUp()
        self.addCleanup(self.ui.doCleanups)
        self.trigger = dict(role='button', name='Lookup')
        flow.live.execute.side_effect = None
        flow.live.execute.return_value = dict(status='delivered_unverified', input_dispatched=True)

    def test_compound_workflow_configures_and_reuses_its_client_for_one_query(self):
        spec = dict(menu=fixture.context()['spec'], trigger=self.trigger, output=None,
                    ready=dict(kind='text', target=dict(role='statictext', name='Progress'), expected='Done'))
        flow.context_menu.execute.side_effect = None
        flow.context_menu.execute.return_value = fixture.delivered_menu()
        with (patch.object(flow.input_policy, '_interactive_desktop', return_value=True),
              patch.object(flow.input_policy, 'prefer_background') as configure,
              patch.object(flow.result_menu, '_matches', side_effect=[False, True, True]),
              patch.object(flow.result_menu.time, 'sleep')):
            report = flow.run(fixture.recipe(dict(action='result-menu', label='Open', context=spec)))
        self.assertTrue(report['steps'][0]['receipt']['menu_path_completed'], report)
        configure.assert_called_once_with(self.ui.uia)
        flow.app.bridge.UIA.assert_called_once()
        self.assertIs(flow.live.execute.call_args.args[1], self.ui.uia)
        self.assertEqual(flow.live.execute.call_count, 1)
        flow.context_menu.execute.assert_called_once()

    def test_legacy_query_uses_its_own_no_autofocus_client_without_restoring(self):
        with patch.object(flow.input_policy, 'prefer_background') as configure:
            receipt = flow.result_menu._query(self.ui.owner, self.trigger, 'Example window')
        configure.assert_called_once_with(self.ui.uia)
        flow.app.visibility.restore_for_action.assert_not_called()
        self.assertTrue(receipt['input_dispatched'])
