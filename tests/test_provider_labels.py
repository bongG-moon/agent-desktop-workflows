"""Missing visibility metadata must not lose an otherwise proven field label."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select
import provider_labels as labels
import learned_tasks as tasks
from test_learned_tasks import demo


class Element:
    def __init__(self, role, box, name='', *, offscreen=None, pid=4, secure=False):
        self.box = dict(zip(('x', 'y', 'width', 'height'), box))
        self.values = {30003: role, 30005: name, 30022: offscreen, 30002: pid, 30019: secure}

    def property(self, key):
        return self.values.get(key)


class LabelTests(unittest.TestCase):
    def setUp(self):
        self.root = Element(50032, (0, 0, 1000, 800))
        self.parent = Element(50026, (0, 0, 900, 700))
        self.label = Element(50020, (50, 80, 110, 20), 'MES Setup')
        self.combo = Element(50003, (170, 80, 150, 20))
        self.binding = Mock(hwnd=8, pid=4)
        self.uia = Mock()
        self.uia.pid.side_effect = lambda e: e.property(30002)
        self.uia.secure.side_effect = lambda e: e.property(30019) is not False
        self.uia.bounds.side_effect = lambda e: e.box
        self.uia.same.side_effect = lambda a, b: a is b
        self.uia.within.return_value = True
        self.uia.related_element.return_value = None
        self.uia.parent.return_value = self.parent
        self.uia.children.return_value = [self.label, self.combo]
        self.shown = patch('provider_visibility._shown', return_value=True)
        self.shown.start()
        self.addCleanup(self.shown.stop)

    def read(self):
        return labels.semantic_label(self.binding, self.uia, self.combo, self.root)

    def test_unknown_visibility_but_owned_on_screen_label_binds_sentence_without_alias(self):
        evidence = self.read()
        self.assertEqual(evidence, {'semantic_label': 'MES Setup', 'label_source': 'sibling_geometry'})
        raw = demo()
        raw['field_label'] = evidence['semantic_label']
        raw['target']['locator']['automation_id'] = 'gcboStatus'
        self.assertEqual(tasks.compile_instruction(raw, 'MES SETUP조건을 N으로 해줘')['value'], 'N')

    def test_hidden_foreign_protected_and_outside_labels_cannot_supply_names(self):
        for property_id, value in ((30022, True), (30002, 99), (30019, True)):
            before = self.label.values[property_id]
            self.label.values[property_id] = value
            self.assertEqual(self.read(), {})
            self.label.values[property_id] = before
        self.label.box['x'] = -10
        self.assertEqual(self.read(), {})

    def test_one_label_must_not_map_to_multiple_fields_or_duplicate_labels(self):
        another = Element(50003, (180, 80, 150, 20))
        self.uia.children.return_value = [self.label, self.combo, another]
        self.assertEqual(self.read(), {})
        self.uia.children.return_value = [self.label, self.combo, Element(50020, (50, 80, 110, 20), 'Other')]
        self.assertEqual(self.read(), {})
