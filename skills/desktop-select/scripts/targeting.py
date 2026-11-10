"""Resolve a user's field or command against one complete, live window tree."""
from collections import defaultdict
from math import isfinite


class SelectionError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


# Structural hints from the host profile, valid only when returned by the
# provider. The table contains no fields, values or company workflow data.
_AREA_IDS = {
    'tsViewCommand': 'commands', 'psView': 'content', 'pnlMainContainer': 'content',
    'pnlConditionContainer': 'conditions', 'pnlConditionArea': 'conditions',
    'pnlConditionButtonArea': 'condition-commands', 'pnlGridArea': 'grid',
    'pnlPropertyArea': 'properties', 'pnlPropertyContainer': 'properties',
}
_AREA_CLASSES = {'FpSpread': 'grid', 'MesGrid': 'grid', 'FXGrid': 'grid',
                 'PropertyGrid': 'properties', 'MesPropertyGrid': 'properties'}
_ALIASES = frozenset(_AREA_IDS.values())
_FIELDS = {'combobox', 'textfield', 'checkbox', 'radiobutton', 'spinner'}
_PASSIVE = {'group', 'statictext'}


def _text(value):
    return value.strip().casefold() if isinstance(value, str) else ''


def _role(value):
    return {'edit': 'textfield', 'text': 'statictext', 'pane': 'group'}.get(_text(value), _text(value))


def _identifier(node):
    native = node.get('native_id')
    if isinstance(native, dict) and native.get('kind') == 'automation_id':
        return native.get('value')
    return node.get('automation_id')


def _alias(node):
    return (_AREA_IDS.get(_identifier(node))
            or _AREA_CLASSES.get(str(node.get('class_name') or '').rsplit('.', 1)[-1]))


def _protected(node):
    return (node.get('secure') is not False
            or any(node.get(key) is True for key in ('protected', 'is_protected', 'is_password', 'password'))
            or _role(node.get('role')) == 'password')


def _tree(nodes):
    paths, children = {}, defaultdict(list)
    for i, node in enumerate(nodes):
        path = node.get('path')
        if not isinstance(path, str) or not path or path in paths:
            raise SelectionError('invalid_tree')
        paths[path] = i
        children[node.get('parent_path')].append(i)
    parents = []
    for node in nodes:
        chain = []
        current = node.get('parent_path')
        while current not in (None, ''):
            if current not in paths or paths[current] in chain or len(chain) > 64:
                raise SelectionError('invalid_tree')
            chain.append(paths[current])
            current = nodes[chain[-1]].get('parent_path')
        parents.append(chain)
    return children, parents


def scope_map(nodes):
    """Aliases ordered from nearest to outermost, indexed like the input nodes."""
    _, parents = _tree(nodes)
    return [list(dict.fromkeys(alias for a in chain if not _protected(nodes[a])
                               if (alias := _alias(nodes[a])))) for chain in parents]


def scopes(nodes, index):
    return scope_map(nodes)[index]


def _rectangle(node):
    bounds = node.get('bounds')
    if not isinstance(bounds, dict) or node.get('bounds_valid') is False:
        return None
    x, y, w, h = (bounds.get(k) for k in ('x', 'y', 'width', 'height'))
    if not all(type(v) in (int, float) and isfinite(v) for v in (x, y, w, h)) or w <= 0 or h <= 0:
        return None
    return x, y, x + w, y + h


def _aligned(a, b):
    if not a or not b:
        return False
    height = min(a[3] - a[1], b[3] - b[1])
    return (min(a[3], b[3]) - max(a[1], b[1]) >= .65 * height
            and 0 <= b[0] - a[2] <= 8 * height)


def _obscures(label_rect, field_rect, obstacle):
    if not obstacle:
        return False
    for rect in (label_rect, field_rect):
        center_x, center_y = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
        if obstacle[0] <= center_x < obstacle[2] and obstacle[1] <= center_y < obstacle[3]:
            return True
    return (obstacle[0] < field_rect[0] and obstacle[2] > label_rect[2]
            and max(label_rect[1], field_rect[1], obstacle[1]) < min(label_rect[3], field_rect[3], obstacle[3]))


def _label(nodes, children, index):
    """Accept a text peer, including a WinForms label wrapped in a group."""
    node = nodes[index]
    if node.get('offscreen') is not False:
        return None
    if _role(node.get('role')) == 'group':
        peers = children.get(node['path'], ())
        if len(peers) != 1 or _protected(node):
            return None
        child = nodes[peers[0]]
        if node.get('name') and _text(node['name']) != _text(child.get('name')):
            return None
        node = child
    if (_role(node.get('role')) == 'statictext' and not _protected(node)
            and node.get('offscreen') is False and _text(node.get('name'))):
        return _text(node['name']), _rectangle(node)
    return None


def _geometry(nodes, children, wanted=None):
    matches = {}
    for peers in children.values():
        # Large virtual lists are not form layouts. Redacted siblings with no
        # bounds also prevent us from ruling out an intervening control.
        if len(peers) > 160 or any(_protected(nodes[i]) and not _rectangle(nodes[i]) for i in peers):
            continue
        labels = {i: label for i in peers if (label := _label(nodes, children, i)) and label[1]}
        if wanted and not any(name == wanted for name, _ in labels.values()):
            continue
        fields = [i for i in peers if _role(nodes[i].get('role')) in _FIELDS]
        boxes = {i: _rectangle(nodes[i]) for i in peers}
        links = defaultdict(list)
        for label, (_, box) in labels.items():
            for field in fields:
                if _aligned(box, boxes[field]) and not any(
                        _obscures(box, boxes[field], boxes[i]) for i in peers if i not in (label, field)):
                    links[label].append(field)
        for label, fields_for_label in links.items():
            if (wanted and labels[label][0] != wanted) or len(fields_for_label) != 1:
                continue
            field = fields_for_label[0]
            if sum(field in linked for linked in links.values()) == 1:
                # Preserve the provider's casing in the user-facing inventory.
                source = nodes[label]
                if _role(source.get('role')) == 'group':
                    source = nodes[children[source['path']][0]]
                matches[field] = source['name']
    return matches


def field_labels(nodes):
    """Observed field labels that can be shown without conflating peer fields."""
    children, parents = _tree(nodes)
    geometry = _geometry(nodes, children)
    result = {}
    same_label = defaultdict(list)
    for i, node in enumerate(nodes):
        if _role(node.get('role')) not in _FIELDS or any(_protected(nodes[j]) for j in (i, *parents[i])):
            continue
        source = node.get('label_source')
        if source == 'labeled_by':
            label = node.get('semantic_label')
        elif source != 'invalid_labeled_by' and i in geometry:
            source, label = 'sibling_geometry', geometry[i]
        else:
            continue
        if _text(label):
            result[i] = {'semantic_label': label, 'label_source': source}
            same_label[node.get('parent_path'), _text(label)].append(i)
    for duplicates in same_label.values():
        if len(duplicates) > 1:
            for i in duplicates:
                result.pop(i)
    return result


def resolve(nodes, query, *, role=None, within=None):
    """Return (index, observed evidence). 'within' always refers to an ancestor."""
    wanted = _text(query)
    if not wanted:
        raise SelectionError('empty_target')
    children, parents = _tree(nodes)
    geometry = _geometry(nodes, children, wanted)
    matches = {}
    aliases = _text(within)
    scope_ids = set()
    if aliases:
        for i, node in enumerate(nodes):
            if aliases == _alias(node) or (aliases not in _ALIASES
                    and aliases in (_text(node.get('name')), _text(_identifier(node)))):
                scope_ids.add(i)
        if not scope_ids:
            raise SelectionError('scope_missing')
        if aliases not in _ALIASES and len(scope_ids) > 1:
            raise SelectionError('scope_ambiguous')
    for i, node in enumerate(nodes):
        kind = _role(node.get('role'))
        if (role and kind != _role(role)) or (aliases and not scope_ids.intersection(parents[i])):
            continue
        if wanted == _text(_identifier(node)):
            matches[i] = 'automation_id'
        elif (role or kind not in _PASSIVE) and wanted == _text(node.get('name')):
            matches[i] = 'name'
        elif node.get('label_source') == 'labeled_by':
            if wanted == _text(node.get('semantic_label')):
                matches[i] = 'labeled_by'
        elif i in geometry and node.get('label_source') != 'invalid_labeled_by':
            matches[i] = 'sibling_geometry'
    if not matches:
        raise SelectionError('target_missing')
    if len(matches) != 1:
        raise SelectionError('target_ambiguous')
    index, evidence = next(iter(matches.items()))
    if any(_protected(nodes[i]) for i in (index, *parents[index])):
        raise SelectionError('target_protected')
    if nodes[index].get('enabled') is not True:
        raise SelectionError('target_disabled')
    return index, evidence
