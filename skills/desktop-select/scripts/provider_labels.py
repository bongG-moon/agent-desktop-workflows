"""Associate display labels without changing a control's execution identity."""
import win_bridge as win
from provider_visibility import visible


FIELDS = {50002, 50003, 50004, 50013, 50016}


def _text(value):
    return (isinstance(value, str) and bool(value.strip()) and len(value.encode('utf-8')) <= 1000
            and not any(ord(c) < 32 for c in value))


def semantic_label(binding, uia, element, root):
    """LabeledBy or a mutually unique visible sibling; never infer from an ID."""
    try:
        if (uia.secure(element) or uia.pid(element) != binding.pid
                or element.property(30003) not in FIELDS or not uia.within(element, root)):
            return {}
        label = uia.related_element(element, 30018)
        if label is not None:
            if uia.pid(label) != binding.pid or uia.secure(label) or not uia.within(label, root):
                return {}
            text = label.property(30005)
            return {'semantic_label': text, 'label_source': 'labeled_by'} if _text(text) else {}
        parent = uia.parent(element)
        if parent is None or not uia.within(parent, root) or uia.secure(parent):
            return {}
        siblings = list(uia.children(parent))
        if len(siblings) > 80:
            return {}
        labels, fields = [], []
        for sibling in siblings:
            if uia.pid(sibling) != binding.pid:
                return {}
            if uia.secure(sibling) or not visible(binding, uia, sibling, root=root):
                continue
            role, box = sibling.property(30003), uia.bounds(sibling)
            if role == 50020:
                text = sibling.property(30005)
                if _text(text):
                    labels.append((box, text))
            elif role in FIELDS:
                fields.append((sibling, box))

        def aligned(label_box, field_box):
            overlap = min(label_box['y'] + label_box['height'], field_box['y'] + field_box['height']) - max(label_box['y'], field_box['y'])
            height = min(label_box['height'], field_box['height'])
            gap = field_box['x'] - label_box['x'] - label_box['width']
            return overlap >= height * .65 and 0 <= gap <= height * 6

        target = [box for candidate, box in fields if uia.same(candidate, element)]
        candidates = [(box, text) for box, text in labels if len(target) == 1 and aligned(box, target[0])]
        if len(candidates) != 1:
            return {}
        box, text = candidates[0]
        if sum(aligned(box, field_box) for _, field_box in fields) != 1:
            return {}
        return {'semantic_label': text, 'label_source': 'sibling_geometry'}
    except win.BridgeError:
        return {}
