"""A transient native toolbar tooltip must not become a replay selector."""
import base64
from contextlib import ExitStack
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'skills/desktop-select/scripts'))
import desktop_select
import toolbar_button as bar
import visual_target as visual
import teach_capture


def glyph(bg, mirrored=False):
    pixels = []
    for y in range(25):
        for x in range(25):
            if mirrored:
                x = 24-x
            color = bg
            if 4 <= x <= 20 and 6 <= y <= 16:
                color = (80,100,90) if x<10 else (170,225,120)
            if x==y and x>12:
                color = (33,50,74)
            pixels.extend(color)
    return bytes(pixels)


class ToolbarButtonTests(unittest.TestCase):
    def test_hover_tint_preserves_each_foreground_pixel(self):
        expected = glyph((230,240,250))
        self.assertTrue(bar.matches(expected, glyph((253,254,255))))
        self.assertFalse(bar.matches(expected, glyph((253,254,255),mirrored=True)))
        self.assertFalse(bar.matches(expected, glyph((140,140,140))))
        spec = dict(surface=bar.SURFACE,rgb=base64.b64encode(expected).decode())
        self.assertEqual(visual.validate_spec(spec),spec)
        for change in (dict(gesture='double-click'),dict(offset=[9,9]),dict(rgb='bad'),
                       dict(rgb=base64.b64encode(bytes(25*25*3)).decode())):
            with self.assertRaises(visual.bridge.BridgeError):
                visual.validate_spec(dict(spec,**change))

    def test_native_toolbar_proof_wins_only_over_transient_button_name(self):
        worker=object.__new__(teach_capture._Worker)
        worker.binding=Mock(hwnd=1,pid=3)
        event=dict(action='click',scope_proven=True,window=1,visual=dict(surface=bar.SURFACE))
        node=dict(role='button',available_actions=['Click'],enabled=True,secure=False,
                  locator=dict(role='button',name='Zoom In'))
        def selected(node):
            return worker._use_visual(event,dict(window=1,node=node))
        self.assertTrue(selected(node))
        self.assertFalse(selected(dict(node,locator=dict(role='button',automation_id='Zoom'))))
        self.assertFalse(selected(dict(node,enabled=False)))
        self.assertFalse(selected(dict(node,secure=True)))

    def test_native_toolbar_search_rejects_second_match_and_skips_disabled(self):
        binding=Mock(hwnd=1,pid=12)
        window=[0,0,175,100]
        root,a,b=Mock(),Mock(),Mock()
        uia=Mock()
        uia.from_handle.side_effect={1:root,2:a,3:b}.get
        def elem(role,enabled=True):
            e=Mock()
            e.property.side_effect={30003:role,30010:enabled}.get
            return e
        a.property.side_effect=b.property.side_effect={30003:50021}.get
        e1,e2,disabled=elem(50000),elem(50000),elem(50000,False)
        uia.pid.return_value=12
        uia.secure.return_value=False
        uia.within.return_value=True
        uia.children.side_effect={a:[e1,disabled], b:[e2]}.get
        bounds={a:[0,0,160,40],b:[0,40,160,40],e1:[50,2,35,35],disabled:[90,2,35,35],e2:[10,42,35,35]}
        wanted=glyph((224,235,245))
        spec=dict(surface=bar.SURFACE,rgb=base64.b64encode(wanted).decode())
        actual=[glyph((250,250,250)),glyph((250,250,250),mirrored=True)]
        with ExitStack() as ctx:
            ctx.enter_context(patch.object(bar,'_toolbars',return_value=[2,3]))
            ctx.enter_context(patch.object(bar.bridge,'UIA')).return_value.__enter__.return_value=uia
            ctx.enter_context(patch.object(bar.vision,'_window_rect',return_value=window))
            ctx.enter_context(patch.object(bar.vision,'_frame',return_value='current'))
            ctx.enter_context(patch.object(bar.vision,'_bounds',side_effect=lambda u,e:bounds[e]))
            ctx.enter_context(patch.object(bar.vision,'_rgb',side_effect=lambda frame,box:actual[box[1]>40]))
            ctx.enter_context(patch.object(visual,'_safe_patch'))
            self.assertEqual(bar.observe(binding,spec)['point'],[67,19])
            actual.reverse()
            self.assertEqual(bar.observe(binding,spec)['point'],[27,59])
            actual[0]=actual[1]
            with self.assertRaisesRegex(bar.bridge.BridgeError,'ambiguous'):
                bar.observe(binding,spec)
