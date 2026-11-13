from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'skills/desktop-select/scripts'))
import desktop_select as app


class ObserveBoundaries(unittest.TestCase):
    def setup_tree(self, count=1):
        elements = [Mock(name='node'+str(i)) for i in range(count)]
        uia, binding = Mock(), Mock(pid=42, hwnd=1)
        uia.from_handle.return_value = elements[0]
        uia.pid.return_value = 42
        uia.secure.return_value = False
        uia.basic.side_effect = lambda e: dict(role='group', automation_id='native', name='')
        uia.children.side_effect = lambda e: elements[elements.index(e)+1:elements.index(e)+2]
        return elements, uia, binding

    def test_embedded_foreign_branch_is_opaque_but_host_controls_remain(self):
        els,uia,binding=self.setup_tree(3)
        uia.children.side_effect=lambda e: els[1:] if e is els[0] else []
        uia.pid.side_effect=lambda e: 99 if e is els[1] else 42
        nodes,_=app.observe(binding,uia,identities_only=True)
        self.assertEqual(nodes[1],dict(path='0/0',parent_path='0',secure=True))
        self.assertFalse(nodes[2]['secure'])
        self.assertNotIn(els[1],[c.args[0] for c in uia.basic.call_args_list])
        self.assertNotIn(els[1],[c.args[0] for c in uia.children.call_args_list])

    def test_nested_native_form_below_old_depth_limit_is_readable(self):
        els,uia,binding=self.setup_tree(28)
        nodes,_=app.observe(binding,uia,identities_only=True)
        self.assertEqual(len(nodes),28)

    def test_depth_is_still_bounded(self):
        els,uia,binding=self.setup_tree(34)
        with self.assertRaisesRegex(app.SelectionError,'incomplete_tree'):
            app.observe(binding,uia,identities_only=True)


if __name__=='__main__': unittest.main()
