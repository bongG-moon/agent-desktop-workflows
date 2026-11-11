"""Register an existing recording once without losing its demonstrated scope."""
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import registered_setup as setup


class RegisteredSetupTests(unittest.TestCase):
    def test_legacy_duplicate_id_respects_recorded_ancestor(self):
        plan = {'schema':1, 'name':'Scoped task', 'exe':'Example.exe', 'window':'Business', 'steps':[
            {'action':'select','target':'state','role':'combobox','match':'automation_id',
             'ancestors':[{'role':'group','automation_id':'left'}], 'within':'left',
             'recorded_effect':'unobserved', 'instruction':{'mode':'fixed','text':'N','value':'N'}}]}
        nodes = [
            {'path':'0','parent_path':'','role':'window','automation_id':'root'},
            {'path':'0/0','parent_path':'0','role':'group','automation_id':'left'},
            {'path':'0/0/0','parent_path':'0/0','role':'combobox','automation_id':'state'},
            {'path':'0/1','parent_path':'0','role':'group','automation_id':'right'},
            {'path':'0/1/0','parent_path':'0/1','role':'combobox','automation_id':'state'}]
        for node in nodes:
            node.update(secure=False, enabled=True)
        elements = [object() for _ in nodes]
        uia = Mock()
        uia.__enter__, uia.__exit__ = Mock(return_value=uia), Mock(return_value=False)
        uia.from_handle.return_value = elements[0]
        uia.basic.side_effect = lambda element: {k:v for k,v in nodes[elements.index(element)].items()
                                                if k in ('role','automation_id')}
        uia.actions.return_value = ['SetValue']
        owner = Mock(pid=17)
        def resolve(binding, api, selector, **kw):
            self.assertEqual(selector['ancestors'], [{'role':'group','automation_id':'left'}])
            return elements[2]
        args = SimpleNamespace(recipe=None, task='Scoped task', resolution=None, hwnd=None, out='unused')
        with ExitStack() as stack:
            def patched(obj, name, **kw):
                return stack.enter_context(patch.object(obj, name, **kw))
            patched(setup.app.tasks, 'load_task', return_value=plan)
            patched(setup.app, 'windows', return_value=[])
            patched(setup.app, 'pick_window', return_value={})
            patched(setup.app.bridge, 'Binding', return_value=owner)
            patched(setup.app.bridge, 'UIA', return_value=uia)
            patched(setup.app, 'guard')
            observed = patched(setup.app, 'observe', return_value=(nodes,elements))
            ancestry = patched(setup.native_targets, 'ancestry', return_value=[elements[2],elements[1],elements[0]])
            patched(setup.native_targets, 'resolve', side_effect=resolve)
            exported = patched(setup.profiles, 'export_skill', return_value={})
            result = setup.register(args)
        self.assertEqual(result['status'], 'registered')
        self.assertFalse(result['input_dispatched'])
        self.assertEqual(observed.call_count, 1)
        self.assertIs(ancestry.call_args.args[1], elements[2])
        self.assertEqual(exported.call_args.args[0]['steps'][0]['value'], 'N')


if __name__ == '__main__':
    unittest.main()
