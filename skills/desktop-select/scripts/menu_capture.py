"""Read the demonstrator's scoped context-menu gesture; never dispatch input."""
from collections import deque
from dataclasses import dataclass
import time

import context_menu as menus
import win_bridge as win


def window_point_hint(binding, event):
    """A window-relative recording hint, unusable as a standalone click target."""
    from row_vision import _window_rect
    if event.get('scope_proven') is not True or event.get('window') != binding.hwnd:
        return None
    try:
        hit = event.get('hit_window')
        if not hit or win.root_window(hit) != binding.hwnd or win.hwnd_pid(hit) != binding.pid:
            return None
        rect = _window_rect(binding)
        point = event['x'], event['y']
        if menus._inside(point, rect):
            return [(coordinate-origin)/extent for coordinate, origin, extent in zip(point, rect, rect[2:])]
    except (OSError, win.BridgeError):
        return None


def capture_surface(binding, uia, event, history):
    """Use the live region, or the exact pre-click hit if a popup now covers it."""
    point = event['x'], event['y']
    if event.get('scope_proven') is not True or event['window'] != binding.hwnd:
        return None
    try:
        return menus.teach_surface(binding, point)
    except OSError:
        return None
    except win.BridgeError as error:
        # Native popups can replace the hit entirely; embedded menus can
        # instead present a non-region peer still inside the original HWND.
        if error.code not in ('context_point_occluded', 'named_context_region_required',
                              'context_point_unproven', 'context_point_outside_region'):
            return None
    # The hook proves the original native receiver at mouse-down. The app can
    # open its menu before this worker reads UIA. Preserve a recording hint
    # from that exact receiver; replay will hit-test it again before input.
    hit = event.get('hit_window')
    try:
        if hit and win.root_window(hit) == binding.hwnd and win.hwnd_pid(hit) == binding.pid:
            binding.check()
            root = uia.from_handle(binding.hwnd)
            receiver = uia.from_handle(hit)
            hint = _surface_hint(binding, uia, receiver, root, point)
            if hint:
                return hint
    except (win.BridgeError, OSError):
        pass
    # A right-button release often opens the popup before the worker sees
    # the down event. Revalidate the previously hit region's identity and
    # protected children without hit-testing through the covering popup.
    for at, obs in reversed(history):
        if not 0 <= event['at'] - at <= 0.8 or not obs:
            continue
        if (obs.get('pointer') != point or obs.get('hit_window') != event['hit_window']
                or obs['window'] != binding.hwnd or obs['node'].get('secure') is not False
                or obs['node'].get('enabled') is not True):
            return None
        item, root = obs['element'], obs['root']
        try:
            binding.check()
            if not uia.same(root, uia.from_handle(binding.hwnd)) or not uia.within(item, root):
                return None
            return _surface_hint(binding, uia, item, root, point)
        except (win.BridgeError, OSError):
            return None
    return None


def _surface_hint(binding, uia, item, root, point):
    for region in menus._surface_candidates(binding, uia, item, root):
        if region.property(30010) is not True:
            return None
        target = menus._surface_target(binding, uia, region)
        if not target:
            continue
        rect = menus._rect(uia, region)
        if not menus._inside(point, rect):
            continue
        try:
            menus._check_region_children(binding, uia, region, point)
        except win.BridgeError as error:
            if error.code == 'context_region_tree_incomplete':
                continue
            raise
        ratios = [(coord-start)/extent for coord, start, extent in zip(point, rect, rect[2:])]
        menus._validate_surface(target, ratios)
        return dict(target=target, point=ratios)
    return None


@dataclass
class Pick:
    item: object
    path: tuple
    frames: set
    hit: int
    at: float
    branch: bool


class MenuGesture:
    def __init__(self, step_id, surface, event, dpi):
        self.step_id = step_id
        self.surface = surface
        self.origin = (event['x'], event['y'], 1, 1)
        self.gap = max(6, round(12 * dpi / 96))
        self.last_seen = event['at']
        self.last_poll = 0
        self.history = deque(maxlen=16)
        self.inventories = deque(maxlen=4)
        self.clicked = None
        self.parents = []
        self.active_frames = set()

    def _path(self, uia, items, chosen):
        frame = chosen.surface, chosen.menu_box
        # Multiple rows or submenus with the same caption are distinguished
        # by the actual pointer hit and relationship to the preceding menu.
        parents = [p for p in reversed(self.parents) if
                   not uia.same(chosen.menu, p.item.menu) and not uia.same(chosen.element, p.item.element)
                   and (uia.within(chosen.element, p.item.element) or
                        (frame not in p.frames and menus._near(p.item.box, chosen.menu_box, self.gap)))]
        parent = parents[0] if parents else None
        actual = []
        path = (*parent.path, chosen.name) if parent else (chosen.name,)
        if parent is None:
            actual = [i for i in items if not uia.same(chosen.element, i.element)
                      and uia.within(chosen.element, i.element)]
            # Some providers expose all nested menu items under the actual
            # opener. This is direct hierarchy even if the user hovered it
            # between worker samples.
            hierarchy = tuple(actual)
            actual.sort(key=lambda i: sum(uia.within(i.element, j.element) for j in hierarchy))
            if not menus._near(self.origin, (actual[0] if actual else chosen).menu_box, self.gap):
                return None
            path = tuple(i.name for i in actual) + (chosen.name,)
        if len(path) > 8:
            return None
        return path, parent, actual

    def observe(self, binding, uia, items, point, hit):
        matches = [item for item in items if menus._inside(point, item.box)]
        if len(matches) != 1 or win.window_from_point(*point) != hit:
            return None
        chosen = matches[0]
        found = self._path(uia, items, chosen)
        if found is None:
            return None
        path, parent, actual = found
        opener = parent.item if parent else (actual[-1] if actual else None)
        menus._hit_item(binding, uia, chosen, opener,
                        {surface for surface, _ in parent.frames} if parent else set(), self.gap)
        if win.window_from_point(*point) != hit:
            return None
        state = chosen.element.property(30070)
        branch = chosen.element.property(30028) is True and state != 3
        self.active_frames.update((item.surface, item.menu_box) for item in [chosen, *actual])
        return Pick(chosen, path, {(i.surface, i.menu_box) for i in items}, hit, time.monotonic(), branch)

    def remember_inventory(self, binding, uia, items):
        """Record all visible native captions before down, including unhovered items."""
        picks = []
        owner = menus._native_menu_owner(binding)
        if owner:
            frames = {(item.surface, item.menu_box) for item in items}
            for item in items:
                if (win.window_class(item.surface) != '#32768' or win.hwnd_pid(item.surface) != binding.pid
                        or win.window_from_point(*item.center) != item.surface):
                    continue
                found = self._path(uia, items, item)
                if found is None:
                    continue
                path, _, _ = found
                state = item.element.property(30070)
                branch = item.element.property(30028) is True and state != 3
                picks.append(Pick(item, path, frames, item.surface, 0, branch))
            if menus._native_menu_owner(binding) != owner:
                picks = []
        completed = time.monotonic()
        self.inventories.append((completed, picks))

    def sample(self, binding, uia, items, point, hit):
        picked = self.observe(binding, uia, items, point, hit)
        self.history.append((time.monotonic(), picked))
        if picked is not None:
            # The most recent item actually hovered at this depth supersedes
            # its siblings. A fresh child frame may open on hover alone.
            previous = next((p for p in self.parents if p.path == picked.path
                             and uia.same(p.item.element, picked.item.element)), None)
            if previous:
                picked.frames = previous.frames
            self.parents = [p for p in self.parents if len(p.path) < len(picked.path)] + [picked]
        return picked

    def at_down(self, binding, uia, event):
        point = event['x'], event['y']
        for when, sample in reversed(self.history):
            if when <= event['at']:
                if (sample and event['at'] - when <= .8 and sample.hit == event['hit_window']
                        and menus._inside(point, sample.item.box)):
                    return sample
                break
        for when, inventory in reversed(self.inventories):
            if when <= event['at']:
                # Native commands may close the menu before the worker sees
                # down. Match the hook's original receiver/point to the newest
                # complete pre-down inventory, never a later picture or caption.
                matches = [pick for pick in inventory if event['at'] - when <= .8
                           and pick.hit == event['hit_window'] and menus._inside(point, pick.item.box)]
                if len(matches) == 1:
                    pick = matches[0]
                    self.active_frames.update(pick.frames)
                    return pick
                break
        try:
            items = menus._menus(binding, uia)
            return self.sample(binding, uia, items, point, event['hit_window'])
        except (win.BridgeError, OSError):
            return None

    def tick(self, binding, uia, now):
        """Poll only during this gesture. Confirm the selected leaf closed its menu."""
        if now - self.last_poll < .12:
            return 'pending'
        self.last_poll = now
        try:
            binding.check()
            items = menus._menus(binding, uia)
            self.remember_inventory(binding, uia, items)
            open_frames = {(i.surface, i.menu_box) for i in items}
            if self.clicked and not self.clicked.branch and not open_frames.intersection(self.active_frames):
                return 'complete'
            if (open_frames.intersection(self.active_frames)
                    or any(menus._near(self.origin, item.menu_box, self.gap) for item in items)):
                self.last_seen = now
                point = win.cursor()
                self.sample(binding, uia, items, point, win.window_from_point(*point))
                return 'pending'
            self.history.append((time.monotonic(), None))
            return 'expired' if now - self.last_seen > 1.5 else 'pending'
        except (win.BridgeError, OSError):
            self.history.append((time.monotonic(), None))
            return 'expired' if now - self.last_seen > 1.5 else 'pending'

    def step(self):
        path = list(self.clicked.path)
        menu = dict(self.surface, path=path)
        menus.validate_spec(menu)
        return dict(action='result-menu', id=self.step_id, label='우클릭 → ' + ' → '.join(path), ordinary_menu=True,
                    context=dict(trigger=None, ready=None, menu=menu, output=None))
