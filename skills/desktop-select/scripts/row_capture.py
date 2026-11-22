"""Recognize a demonstrated click inside a painted UIA row, using local samples."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import threading
import time

import row_vision as visual
import win_bridge as bridge


def _crop(pixels, width, box):
    x, y, w, h = box
    stride, start = width * 3, (y * width + x) * 3
    starts = range(start, start + h * stride, stride)
    return b''.join(pixels[offset:offset + w * 3] for offset in starts)


@dataclass
class Frame:
    window: list
    rows: list
    pixels: list
    start: float
    end: float


def sample(binding):
    """Retain cropped, unprotected row pixels in RAM, never window screenshots."""
    start = time.monotonic()
    window, rows = visual._row_layout(binding)
    # Bound live samples on large grids; ordinary semantic recording continues.
    if not rows or len(rows) > 64 or sum(r[2] * r[3] for r, _ in rows) > 4_000_000:
        return None
    shot = window, rows, visual._frame(binding, window)
    if visual._row_layout(binding) != (window, rows):
        return None
    return Frame(window, rows, [visual._rgb(shot, rect) for rect, _ in rows], start, time.monotonic())


def _regions(width, height, first, second=None):
    """Group nearby glyph columns into text/icon regions without OCR or app names."""
    inset = max(1, height // 12)
    top, bottom = inset, height - inset
    min_y = [bottom] * width
    max_y = [-1] * width
    for y in range(top, bottom):
        line = first[y * width:(y + 1) * width]
        if second is not None:
            line = bytes(a | b for a, b in zip(line, second[y * width:(y + 1) * width]))
        # Borders and selection outlines do not supply identities or states.
        if line.count(1) > width * .65:
            continue
        x = line.find(b'\x01', 2, width - 2)
        while x >= 0:
            min_y[x], max_y[x] = min(min_y[x], y), y
            x = line.find(b'\x01', x + 1, width - 2)
    used = [x for x in range(width) if max_y[x] >= 0]
    groups = []
    for x in used:
        if groups and x - groups[-1][-1] <= max(3, min(18, height // 8)):
            groups[-1].append(x)
        else:
            groups.append([x])
    for columns in groups:
        left, right = max(0, columns[0] - 2), min(width, columns[-1] + 3)
        y1 = max(0, min(min_y[x] for x in columns) - 2)
        y2 = min(height, max(max_y[x] for x in columns) + 3)
        box = [left, y1, right - left, y2 - y1]
        if 3 <= box[2] <= 512 and 3 <= box[3] <= 128 and box[2] * box[3] <= 32768:
            yield box


def _button(point, rect):
    x, y = point[0] - rect[0], point[1] - rect[1]
    radius = min(24, max(4, rect[3] // 6), x, y, rect[2] - x - 1, rect[3] - y - 1)
    if radius < 2:
        return None
    return [x - radius, y - radius, 2 * radius + 1, 2 * radius + 1]


@dataclass
class Press:
    rect: list
    meta: dict
    number: int
    window: list
    pixels: bytes
    keys: list
    button: list
    at: float
    button_mask: str

    def click_spec(self):
        """Keep the two small witnessed marks, even if no result appears in this row."""
        key = self.keys[0]
        return dict(version=2, size=self.rect[2:], ordinal=self.number, boxes=dict(key=key, button=self.button),
                    key=visual._pattern(_crop(self.pixels, self.rect[2], key)), button=self.button_mask, **self.meta)


def prepare(frame, point, at, *, cue=None):
    """Match the pre-down row; retain the smaller button mark even if rows look alike."""
    if frame is None or not 0 <= at - frame.end <= 2.5:
        return None
    rect, meta = visual._point_row(frame.rows, point)
    index = frame.rows.index((rect, meta))
    siblings = [i for i, (_, m) in enumerate(frame.rows) if m['scope'] == meta['scope'] and m['role'] == meta['role']]
    peers = [i for i in siblings if frame.rows[i][0][2:] == rect[2:]]
    number = sorted(siblings, key=lambda i: (frame.rows[i][0][1], frame.rows[i][0][0])).index(index) + 1
    button = _button(point, rect)
    if not button:
        return None
    pixels = frame.pixels[index]
    w, h = rect[2:]
    button_mask = visual._pattern(_crop(pixels,w,button))
    keys = []
    for box in _regions(w,h,visual._glyph(pixels)):
        if visual._overlaps(box, button):
            continue
        bits = visual._glyph(_crop(pixels,w,box))
        if bits.count(1) < 8 or bits.count(0) < 8:
            continue
        if all(i == index or visual._match_distance(bits,visual._glyph(_crop(frame.pixels[i],w,box)),box[2]) > .22
               for i in peers):
            keys.append(box)
    if not keys:
        if cue is not None:
            cue.update(button_box=button, button=button_mask)
        return None
    return Press(rect, meta, number, frame.window, pixels, keys, button, at, button_mask)


def infer(press, frame):
    """Only finalize when an independent, non-button state changed on the same row."""
    if frame.window != press.window or frame.start <= press.at:
        return None
    candidates = [i for i, (rect, meta) in enumerate(frame.rows) if rect == press.rect and meta == press.meta]
    if len(candidates) != 1:
        return None
    index = candidates[0]
    current, previous = frame.pixels[index], press.pixels
    w,h = press.rect[2:]
    peers = [i for i, (rect,meta) in enumerate(frame.rows)
             if rect[2:] == [w,h] and meta == press.meta]
    key = None
    for candidate in press.keys:
        old = visual._glyph(_crop(previous,w,candidate))
        matches = [i for i in peers if visual._match_distance(
            old,visual._glyph(_crop(frame.pixels[i],w,candidate)),candidate[2]) <= .20]
        if matches == [index] and visual._match_distance(old,visual._glyph(_crop(current,w,candidate)),candidate[2]) <= .14:
            key = candidate
            break
    if key is None:
        return None
    scores = []
    for box in _regions(w,h,visual._glyph(previous),visual._glyph(current)):
        if visual._overlaps(key,box) or visual._overlaps(press.button,box):
            continue
        before, after = visual._glyph(_crop(previous,w,box)), visual._glyph(_crop(current,w,box))
        if min(before.count(0),before.count(1),after.count(0),after.count(1)) < 8:
            continue
        if visual._difference(before,after) >= .30:
            scores.append(box)
    # Multiple independent changes might be timers, selection or animation.
    # A demonstrator can identify the intended completion marker in row teaching.
    if len(scores) != 1:
        return None
    state = scores[0]
    spec = dict(version=1, size=[w,h], boxes=dict(key=key,button=press.button,state=state),
                key=visual._pattern(_crop(previous,w,key)), button=press.button_mask,
                before=visual._pattern(_crop(previous,w,state)), after=visual._pattern(_crop(current,w,state)),
                **press.meta)
    visual.validate_spec(spec)
    return spec


class Tracker(threading.Thread):
    """Sample outside the hook and recorder event loop; update only the original step."""
    def __init__(self, binding, model):
        super().__init__(name='DesktopSelectRowSampler', daemon=True)
        self.binding,self.model = binding,model
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.initial_sample = threading.Event()
        self.interest = threading.Event()
        self.history = deque(maxlen=2)
        self.latest = None
        self.pinned = deque(maxlen=3)
        self.pending = {}
        self.last_transition = 0.

    def pin_down(self, event):
        """Hook thread: retain a frame reference only. No image work, COM or locks."""
        frame = self.latest
        if frame and 0 <= event['at'] - frame.end <= 2.5:
            self.pinned.append((event['token'], frame))

    def before(self, at, token=None):
        with self.lock:
            if token is not None:
                for key, frame in tuple(self.pinned):
                    if key == token and self.last_transition < frame.start and 0 <= at - frame.end <= 2.5:
                        return frame
            return next((f for f in reversed(self.history) if 0 <= at-f.end <= 2.5), None)

    def invalidate(self, at):
        """A scroll may exchange two rows at the same physical coordinates."""
        with self.lock:
            self.last_transition = max(at,self.last_transition)
            self.history.clear()
            self.latest = None
            self.pinned.clear()
            self.pending.clear()
        self.interest.set()

    def next_press(self, point):
        with self.lock:
            for step_id,data in list(self.pending.items()):
                if visual._inside([*point,1,1],data['press'].rect):
                    # The second click cannot provide the first click's effect.
                    del self.pending[step_id]

    def follow(self, step_id, press):
        with self.lock:
            # Only row crops from witnessed clicks survive beyond two samples.
            if (len(self.pending) < 32 and
                    sum(len(data['press'].pixels) for data in self.pending.values()) + len(press.pixels) <= 24_000_000):
                self.pending[step_id] = dict(press=press, candidate=None, published=None, since=0.)

    def cancel_step(self, step_id):
        with self.lock:
            self.pending.pop(step_id,None)

    def observe(self, frame):
        with self.lock:
            if frame.start <= self.last_transition or self.stop_event.is_set():
                return
            previous = self.history[-1] if self.history else None
            self.history.append(frame)
            self.latest = frame
            work = list(self.pending.items())
        # Stable screens are common while a second instance is starting. An
        # exact byte match across all peers proves identity and pixels have
        # not changed, without segmenting those rows again on every poll.
        unchanged = (previous is not None and frame.window == previous.window
                     and frame.rows == previous.rows and frame.pixels == previous.pixels)
        for step_id, data in work:
            press = data['press']
            if frame.end < press.at or self.stop_event.is_set():
                continue
            if unchanged and data.get('last_end') == previous.end:
                spec = data['candidate']
            else:
                try:
                    spec = infer(press, frame)
                except bridge.BridgeError:
                    spec = None
            with self.lock:
                if self.pending.get(step_id) is not data or self.stop_event.is_set():
                    continue
                data['last_end'] = frame.end
                # A later change invalidates transient states until a new one
                # settles. A double click replaces and cancels this step.
                if spec != data['candidate']:
                    data.update(candidate=spec, since=frame.end)
                    if data['published']:
                        self.model.row_step(step_id,press.number,None)
                        data['published'] = None
                elif spec and not data['published'] and frame.start-data['since'] >= .65:
                    self.model.row_step(step_id,press.number,spec)
                    data['published'] = spec
                if frame.end-press.at > 90:
                    self.pending.pop(step_id,None)

    def run(self):
        misses = 0
        try:
            while not self.stop_event.is_set():
                try:
                    frame = sample(self.binding)
                    if frame and not self.stop_event.is_set():
                        self.observe(frame)
                except (bridge.BridgeError, OSError, ValueError):
                    frame = None
                if frame:
                    self.initial_sample.set()
                    misses = 0
                    self.stop_event.wait(.30)
                else:
                    with self.lock:
                        self.history.clear()
                        self.latest = None
                    misses += 1
                    # A changing layout can invalidate the first PrintWindow
                    # sample. Retry it before reporting recording readiness.
                    if misses >= 3:
                        self.initial_sample.set()
                    self.interest.wait(.12 if misses < 3 else 30)
                    self.interest.clear()
        finally:
            with self.lock:
                self.history.clear()
                self.latest = None
                self.pinned.clear()
                self.pending.clear()

    def stop(self):
        self.stop_event.set()
        self.interest.set()
