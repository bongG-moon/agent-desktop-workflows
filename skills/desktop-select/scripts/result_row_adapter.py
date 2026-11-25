"""Optional local Windows OCR for an identified, ruled result grid with a known row count.

The proof belongs to one live observation. Never serialize it or reuse it on another run.
"""
from base64 import b64encode
from bisect import bisect_right
from dataclasses import dataclass
from functools import partial
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import time

import grounding
import result_cell
import result_trial
import row_vision as vision
import visual_target
import win_bridge as win


# Use the OS language packs and WinRT projection in Windows PowerShell 5.1.
# No executable text comes from the recipe, the table, or its cells. Only the
# cropped PNG travels over stdin. Neither the image nor OCR text is persisted.
_RECOGNIZE = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.InMemoryRandomAccessStream,Windows.Storage.Streams,ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.DataWriter,Windows.Storage.Streams,ContentType=WindowsRuntime]
$method = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetGenericArguments().Count -eq 1 -and
    $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1
function Await($operation, $type) {
    $task = $method.MakeGenericMethod($type).Invoke($null, @($operation))
    $task.Wait()
    $task.Result
}
$engine = $null
if ([Console]::In.ReadLine() -eq 'en') {
    $english = [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages |
        Where-Object { $_.LanguageTag -match '^en(-|$)' } | Select-Object -First 1
    if ($english) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($english) }
}
if (-not $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
if (-not $engine) { exit 3 }
$stream = [Windows.Storage.Streams.InMemoryRandomAccessStream]::new()
$writer = [Windows.Storage.Streams.DataWriter]::new($stream)
$writer.WriteBytes([Convert]::FromBase64String([Console]::In.ReadToEnd()))
$null = Await $writer.StoreAsync() ([uint32])
$null = $writer.DetachStream()
$stream.Seek(0)
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await $decoder.GetSoftwareBitmapAsync() ([Windows.Graphics.Imaging.SoftwareBitmap])
$result = Await $engine.RecognizeAsync($bitmap) ([Windows.Media.Ocr.OcrResult])
$words = @(foreach ($line in $result.Lines) {
    foreach ($word in $line.Words) {
        $r = $word.BoundingRect
        @{text=$word.Text; x=$r.X; y=$r.Y; width=$r.Width; height=$r.Height}
    }
})
@{language=$engine.RecognizerLanguage.LanguageTag; angle=$result.TextAngle; words=$words} | ConvertTo-Json -Depth 4 -Compress
"""


def _need(ok, reason):
    if not ok:
        raise win.BridgeError(reason)


def _ocr(png, *, latin):
    _need(os.name == 'nt', 'result_ocr_unavailable')
    shell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    try:
        run = subprocess.run([str(shell), '-NoProfile', '-NonInteractive', '-Command', _RECOGNIZE],
                             input=(b'en\n' if latin else b'profile\n') + b64encode(png),
                             capture_output=True, timeout=8,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired as error:
        raise win.BridgeError('result_ocr_timeout') from error
    except OSError as error:
        raise win.BridgeError('result_ocr_unavailable') from error
    _need(run.returncode != 3, 'result_ocr_language_unavailable')
    _need(run.returncode == 0 and len(run.stdout) <= 512_000, 'result_ocr_unavailable')
    try:
        data = json.loads(run.stdout)
        _need(isinstance(data['words'], list) and len(data['words']) <= 4096, 'result_ocr_scan_limit')
        # Rotated OCR rectangles use a different coordinate system.
        _need(data['angle'] in (None, 0), 'result_ocr_layout_unavailable')
        return data['language'], data['words']
    except (ValueError, KeyError, TypeError) as error:
        raise win.BridgeError('result_ocr_unavailable') from error


def _rules(pixels, *, length, stride, sample_stride, start, stop):
    """Positions of thin rules darker than both sides over most of the sampled span."""
    samples = [sample_stride * (start + (stop-start-1) * i // 30) for i in range(31)]
    rules = []
    for pos in range(2, length - 2):
        center = pos * stride
        votes = 0
        for offset in samples:
            i = center + offset
            if min(pixels[i-2*stride], pixels[i+2*stride]) - pixels[i] >= 14:
                votes += 1
        if votes >= 22:
            if rules and pos - rules[-1] <= 2:
                rules[-1] = pos
            else:
                rules.append(pos)
    return rules


def _grid(raw, width, height):
    _need(len(raw) == width*height*4, 'result_ocr_frame_invalid')
    green = raw[1::4]
    ys = _rules(green, length=height, stride=width, sample_stride=1, start=0, stop=width)
    _need(3 <= len(ys) <= 130, 'result_ocr_layout_unavailable')
    xs = _rules(green, length=width, stride=1, sample_stride=width, start=ys[0], stop=ys[-1]+1)
    _need(2 <= len(xs) <= 65, 'result_ocr_layout_unavailable')
    _need(all(b-a >= 10 for a, b in zip(ys, ys[1:])) and all(b-a >= 12 for a, b in zip(xs, xs[1:])),
          'result_ocr_layout_unavailable')
    return xs, ys


def _without_rules(raw, width, height, xs, ys):
    # Erase only detected divider pixels in the transient OCR input. The proof
    # and final pre-click comparison retain the untouched captured pixels.
    img = bytearray(raw)
    for y in ys:
        start = (y-1)*width*4
        img[start:start+3*width*4] = b'\xff'*(3*width*4)
    for y in range(height):
        for x in xs:
            i = (y*width + x-1)*4
            img[i:i+12] = b'\xff'*12
    return grounding._png(width, height, img)


def _all_ink_read(raw, width, bounds, parts):
    """Reject uncovered glyphs, for example a suffix omitted by the recognizer."""
    left, top, right, bottom = bounds
    def color(x,y):
        i = (y*width+x)*4
        return raw[i:i+3]

    corners = [color(x,y) for x in (left+4, right-4) for y in (top+4, bottom-4)]
    background = max(corners, key=corners.count)
    _need(corners.count(background) >= 3, 'result_ocr_cell_unreadable')

    # At high DPI antialiasing can extend a ruled border an extra pixel or
    # two. Exclude only continuous non-background stripes along that border.
    def border(start, end, stride):
        if raw[start:start+3] == background:
            return False
        return all(len(set(raw[start+ch:end+ch:stride])) == 1 for ch in range(3))

    x0, x1, y0, y1 = left+2, right-2, top+2, bottom-2
    while x0 < left+4 and border(((top+4)*width+x0)*4, ((bottom-3)*width+x0)*4, width*4):
        x0 += 1
    while x1 > right-4 and border(((top+4)*width+x1)*4, ((bottom-3)*width+x1)*4, width*4):
        x1 -= 1
    while y0 < top+4 and border((y0*width+x0)*4, (y0*width+x1+1)*4, 4):
        y0 += 1
    while y1 > bottom-4 and border((y1*width+x0)*4, (y1*width+x1+1)*4, 4):
        y1 -= 1
    for y in range(y0, y1+1):
        intervals = [(max(x0, int(x)-1), min(x1+1, int(x+w)+2))
                     for x,t,w,h,_ in parts if t-1 <= y <= t+h+1]
        start = x0
        for stop, end in [*intervals, (x1+1, x1+1)]:
            if start < stop:
                lo, hi = (y*width+start)*4, (y*width+stop)*4
                for ch, bg in enumerate(background):
                    strip = raw[lo+ch:hi:4]
                    _need(min(strip) >= bg-24 and max(strip) <= bg+24, 'result_ocr_cell_unreadable')
            start = max(start,end)


_CONFUSABLES = str.maketrans({'o': '0', 'i': '1', 'l': '1'})


def _lookalike(value):
    # Only veto a competing row that OCR may have split or confused. An
    # approximate match never supplies either the value or its click position.
    return ''.join(value.lower().split()).translate(_CONFUSABLES)


def _match(xs, ys, words, column, expected, total, evidence, check_cell=None):
    """Assign whole OCR words to fully enclosed cells; never match a substring."""
    cells = {}
    for word in words:
        try:
            left, top, width, height = (word[k] for k in ('x','y','width','height'))
            value = word['text']
            _need(isinstance(value,str) and value and width > 0 and height > 0, 'result_ocr_layout_unavailable')
            row, col = bisect_right(ys, top)-1, bisect_right(xs, left)-1
        except (KeyError, TypeError) as error:
            raise win.BridgeError('result_ocr_layout_unavailable') from error
        # Text outside the border could be a clipped row or column.
        _need(0 <= row < len(ys)-1 and 0 <= col < len(xs)-1 and top > ys[row] and left > xs[col]
              and top+height < ys[row+1] and left+width < xs[col+1], 'result_ocr_layout_unavailable')
        cells.setdefault((row, col), []).append((left, top, width, height, value))

    def full_text(parts):
        parts.sort()
        _need(max(y for _,y,_,_,_ in parts) < min(y+h for _,y,_,h,_ in parts), 'result_ocr_layout_unavailable')
        return ' '.join(part[-1] for part in parts)

    matches = [col for (r,col), parts in cells.items() if r == 0
               and full_text(parts).strip().casefold() == column.strip().casefold()]
    _need(len(matches) == 1, 'result_column_ambiguous' if matches else 'result_column_unavailable')
    col, rows = matches[0], sorted({r for r, _ in cells if r > 0})
    if check_cell:
        check_cell((xs[col], ys[0], xs[col+1], ys[1]), cells[0,col])
    evidence.update(rows_read=len(rows), reported_rows=total, column_index=col)
    _need(len(rows) == total, 'result_ocr_incomplete_rows')
    found, similar = [], 0
    spelling = _lookalike(expected)
    for row in rows:
        parts = cells.get((row, col))
        _need(parts, 'result_ocr_incomplete_rows')
        value = full_text(parts)
        if check_cell:
            check_cell((xs[col], ys[row], xs[col+1], ys[row+1]), parts)
        if value == expected:
            found.append(row)
        elif _lookalike(value) == spelling:
            similar += 1
    evidence['matching_rows'] = len(found)
    _need(len(found) == 1, 'result_value_ambiguous' if found else 'result_value_mismatch')
    if similar:
        evidence['visually_similar_rows'] = similar
        raise win.BridgeError('result_ocr_identity_uncertain')
    row = found[0]
    # Click the OCR glyphs of that exact cell so the final patch includes the ID.
    parts = cells[row, col]
    x = (min(p[0] for p in parts) + max(p[0]+p[2] for p in parts)) // 2
    y = (min(p[1] for p in parts) + max(p[1]+p[3] for p in parts)) // 2
    evidence.update(matched_row_index=row-1, observed_value=expected)
    return int(x), int(y)


@dataclass(frozen=True)
class _Snapshot:
    hwnd: int
    pid: int
    input_tick: int
    window: tuple
    table_bounds: tuple
    crop: tuple
    digest: bytes


@dataclass(frozen=True)
class _Proof(_Snapshot):
    point: tuple
    spec: dict


def _capture(binding, uia, table):
    window = vision._window_rect(binding)
    root = uia.from_handle(binding.hwnd)
    _need(result_cell.bare_group(uia, table) and result_cell.readable_surface(binding, uia, table, root)
          and uia.pid(table) == binding.pid and uia.within(table, root)
          and not uia.secure(table) and table.property(30010) is True, 'result_ocr_surface_changed')
    box = uia.bounds(table)
    raw_bounds = tuple(box[k] for k in ('x','y','width','height'))
    visible = result_trial.visible_table_box(visual_target._client_rect(binding), box)
    crop = tuple(visible[k] for k in ('x','y','width','height'))
    _need(crop[2]*crop[3] <= 2_000_000 and min(crop[2:]) >= visual_target.SIDE, 'result_ocr_scan_limit')
    visual_target._safe_patch(binding, uia, crop, crop[:2], exposed=False, gesture='double-click')
    return tuple(window), raw_bounds, crop, vision._frame(binding, window, crop=crop)


def changed(binding, uia, table, snapshot, guard):
    """Test a previous OCR miss once against the same surface without running OCR."""
    guard()
    _need((binding.hwnd,binding.pid) == (snapshot.hwnd,snapshot.pid), 'result_ocr_surface_changed')
    _need(win.input_tick() == snapshot.input_tick, 'foreground_or_input_changed')
    window, box, crop, raw = _capture(binding, uia, table)
    guard()
    _need(win.input_tick() == snapshot.input_tick, 'foreground_or_input_changed')
    _need((window,box,crop) == (snapshot.window,snapshot.table_bounds,snapshot.crop), 'result_ocr_surface_changed')
    return sha256(raw).digest() != snapshot.digest


def observe(binding, uia, table, column, expected, guard, *, row_count):
    """Return a transient proof only when all reported rows and the exact column are readable."""
    start = time.monotonic()
    evidence = dict(strategy='windows_ocr', requested_value=expected, column=column, reported_rows=row_count)
    try:
        guard()
        _need(type(row_count) is int and 0 <= row_count <= 128, 'result_ocr_row_count_unavailable')
        _need(row_count != 0, 'result_value_missing')
        tick = win.input_tick()
        window, box, crop, raw = _capture(binding, uia, table)
        evidence['search_bounds'] = list(crop)
        width, height = crop[2:]
        xs, ys = _grid(raw, width, height)
        language, words = _ocr(_without_rules(raw, width, height, xs, ys),
                               latin=column.isascii() and expected.isascii())
        evidence['language'] = language
        try:
            x, y = _match(xs, ys, words, column, expected, row_count, evidence, partial(_all_ink_read, raw, width))
        except win.BridgeError as error:
            if error.code != 'result_value_mismatch':
                raise
            result = dict(status='blocked', diagnostic=error.code,
                          _miss_snapshot=_Snapshot(binding.hwnd,binding.pid,tick,window,box,crop,sha256(raw).digest()))
        else:
            point = (crop[0]+x, crop[1]+y)
            spec = visual_target._frame_patch(crop, raw, point)
            spec['gesture'] = 'double-click'
            proof = _Proof(binding.hwnd, binding.pid, tick, window, box, crop, sha256(raw).digest(), point, spec)
            result = dict(status='verified', diagnostic='result_value_matched', proof=proof,
                          value=expected, value_evidence=dict(status='observed', source='windows_ocr'))
        guard()
        _need(win.input_tick() == tick, 'foreground_or_input_changed')
    except (win.BridgeError, OSError, ValueError) as error:
        result = dict(status='blocked', diagnostic=getattr(error, 'code', 'result_ocr_unavailable'))
    evidence['elapsed_seconds'] = round(time.monotonic()-start, 4)
    return dict(result, input_dispatched=False, result_observation=evidence)


def execute(binding, uia, table, proof, guard, *, check_count=None):
    """Use the standard pointer reservation and dispatch guards on the witnessed pixels."""
    def unchanged():
        guard()
        _need(win.input_tick() == proof.input_tick, 'foreground_or_input_changed')

    def validate(point):
        unchanged()
        if check_count is not None:
            check_count()
        _need(binding.hwnd == proof.hwnd and binding.pid == proof.pid and tuple(point) == proof.point,
              'result_ocr_surface_changed')
        window, box, crop, raw = _capture(binding, uia, table)
        _need((window, box, crop) == (proof.window, proof.table_bounds, proof.crop)
              and sha256(raw).digest() == proof.digest, 'result_frame_changed')
        result_cell._point_target(binding, uia, table, point)

    return visual_target.execute(binding, proof.spec, unchanged, before_input=validate, reviewed_point=proof.point)
