"""Correlate local runtime intervals with a completed Codex turn. No UI or model calls."""
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
from uuid import UUID, uuid4


_current = ContextVar('desktop_select_timing', default=None)
_SETUP = {'recorder', 'record', 'review', 'register', 'recover', 'instruct', 'plan'}
_TRIAL = {'try-recording', 'inspect-trial', 'replay'}
_RESUME = {'resume-trial', 'resume-menu-trial', '--resume-detail', '--resume-input', '--resume-menu',
           '--confirm-result-locally', '--cancel-run'}


def trace_dir(thread):
    from storage_paths import default_store
    return default_store().parent / 'timings' / str(UUID(thread))


def invoke(fn, argv, *args):
    """Time public/worker entry points without storing commands, parameters, or outputs."""
    thread = os.getenv('CODEX_THREAD_ID')
    tool = os.getenv('CODEX_TOOL_CALL_ID', '')
    if not thread or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', tool):
        return fn(*args)
    argv = argv[1:] if argv[:1] == ['--_worker'] else argv
    options = set(argv)
    if _RESUME & options:
        phase = 'resume'
    elif '--input-plan' in options or '--read-only' in options:
        phase = 'preflight'
    elif argv and argv[0] in _SETUP:
        phase = 'setup'
    elif argv and argv[0] in _TRIAL:
        phase = 'trial'
    elif argv and argv[0] in ('run', '_plan'):
        phase = 'runner'
    else:
        return fn(*args)
    start = time.monotonic()
    epoch = time.time_ns() // 1_000_000
    spans = []
    trace = dict(phase=phase, start_ms=epoch, spans=spans)
    token = _current.set((start, epoch, trace))
    try:
        return fn(*args)
    finally:
        trace['end_ms'] = epoch + round((time.monotonic() - start) * 1000)
        _current.reset(token)
        try:
            from learned_tasks import _directory
            directory = _directory(trace_dir(thread) / tool)
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / (uuid4().hex + '.json')).open('x', encoding='ascii') as out:
                json.dump(trace, out, separators=(',', ':'))
        except (OSError, ValueError, RuntimeError):
            pass  # An unavailable timing store cannot change an input-delivery receipt.


def record_span(name, started, ended):
    current = _current.get()
    if current:
        base, epoch, trace = current
        trace['spans'].append([name, epoch + round((started-base)*1000), epoch + round((ended-base)*1000)])


def record_steps(starts, rows):
    for row in rows:
        step = row['number']
        if step in starts and 'elapsed_seconds' in row:
            record_span(f'step_{step}', starts[step], starts[step] + row['elapsed_seconds'])


def receipt(report):
    current = _current.get()
    if current:
        trace = current[2]
        identity = report.get('run_id') or report.get('trial_receipt')
        if isinstance(identity, str) and re.fullmatch(r'[0-9a-f]{32,64}', identity):
            trace['run_id'] = identity
        status = report.get('status')
        if isinstance(status, str) and re.fullmatch(r'[a-z_]{1,40}', status):
            trace['status'] = status


def partition(start, end, spans):
    """Attribute each millisecond once, including concurrent tools and nested workers."""
    clipped = [(p, name, max(a, start), min(b, end)) for p, name, a, b in spans
               if a < end and start < b and a < b]
    bounds = sorted({start, end, *(v for _, _, a, b in clipped for v in (a, b))})
    rows = []
    for left, right in zip(bounds, bounds[1:]):
        active = [(p, name) for p, name, a, b in clipped if a <= left and right <= b]
        priority = max((p for p, _ in active), default=0)
        names = sorted({name for p, name in active if p == priority})
        phase = ' + '.join(names) or 'host_processing_or_wait'
        if rows and rows[-1]['phase'] == phase:
            rows[-1]['end_ms'] = right
            rows[-1]['elapsed_ms'] += right - left
        else:
            rows.append(dict(phase=phase, start_ms=left, end_ms=right, elapsed_ms=right-left))
    return rows


def turn_report(path, directory, turn=None):
    """Read only event metadata for reporting. Command output and message text stay local."""
    current = None
    latest = matched = None
    tool_dirs = {p.name for p in directory.iterdir() if p.is_dir()} if directory.is_dir() else set()
    # Skip large response bodies without JSON-decoding them. Codex JSONL places type before payload.
    with path.open('rb') as stream:
        for line in stream:
            header = line[:350]
            if current and (not turn or turn == current['id']) and b'"response_item"' in header[:160] and any(
                    kind in header for kind in (b'"function_call"', b'"custom_tool_call"',
                                                 b'"function_call_output"', b'"custom_tool_call_output"')):
                event = json.loads(line)
                payload = event['payload']
                stamp = round(datetime.fromisoformat(event['timestamp'].replace('Z', '+00:00')).timestamp()*1000)
                key = payload.get('call_id')
                if payload['type'] in ('custom_tool_call', 'function_call'):
                    current['dispatch'][key] = stamp
                elif key in current['dispatch']:
                    # Keep only receipt identifiers, including JSON nested in a shell wrapper.
                    ids = set(re.findall(r'"(?:run_id|trial_receipt)"\s*:\s*"([a-f0-9]{32,64})"',
                                         str(payload.get('output', '')).replace('\\', '')))
                    if ids:
                        current['returns'].append((current['dispatch'][key], stamp, ids))
                continue
            if b'"event_msg"' not in header[:160] or not any(kind in header for kind in
                    (b'"task_started"', b'"item_completed"', b'"task_complete"')):
                continue
            event = json.loads(line)
            data = event['payload']
            kind = data.get('type')
            if kind == 'task_started':
                stamp = datetime.fromisoformat(event['timestamp'].replace('Z', '+00:00'))
                current = dict(id=data.get('turn_id'), received=round(stamp.timestamp()*1000),
                               start_source='host_turn_started', ready=None, tools=[], dispatch={}, returns=[])
            if not current or (turn and turn != current['id']) or data.get('turn_id') != current['id']:
                continue
            if kind == 'item_completed':
                item = data.get('item', {})
                a, b = data.get('started_at_ms'), data.get('completed_at_ms')
                if not isinstance(a, int) or not isinstance(b, int) or b < a:
                    continue
                if item.get('type') == 'UserMessage' and current['start_source'] == 'host_turn_started':
                    current['received'] = a
                    current['start_source'] = 'host_user_message'
                elif item.get('type') == 'CommandExecution':
                    current['tools'].append((item.get('id', ''), a, b))
                elif item.get('type') == 'AgentMessage' and item.get('phase') == 'final_answer':
                    current['ready'] = b
            elif kind == 'task_complete':
                if current['received'] is not None and current['ready'] is not None:
                    latest = current
                    if any(tool in tool_dirs for tool, _, _ in current['tools']):
                        matched = current
                if turn:
                    break
                current = None
    latest = matched or latest
    if not latest:
        return dict(status='unavailable', diagnostic='completed_turn_with_reply_required')
    start, end = latest['received'], latest['ready']
    if end < start:
        return dict(status='unavailable', diagnostic='inconsistent_host_clock')
    spans, calls = [], []
    for tool, a, b in latest['tools']:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', tool):
            continue
        executions = []
        for file in sorted((directory / tool).glob('*.json')):
            data = json.loads(file.read_text(encoding='ascii'))
            phase, left, right = data['phase'], data['start_ms'], data['end_ms']
            if right < start or left > end:
                continue
            executions.append(data)
            spans.append((3 if phase == 'runner' else 2, phase, left, right))
            for name, begin, finish in data['spans']:
                suffix = ('/' + data['run_id'][:8]) if 'run_id' in data else ''
                spans.append((5 if name.startswith('step_') else 4, name + suffix, begin, finish))
        if executions:
            right = max(item['end_ms'] for item in executions)
            ids = {item['run_id'] for item in executions if 'run_id' in item}
            matches = [(sent, received) for sent, received, output_ids in latest['returns']
                       if sent <= a and right <= received and ids & output_ids]
            sent, received = matches[0] if len(matches) == 1 else (None, None)
            calls.append(dict(phases=sorted({item['phase'] for item in executions}), skill_invoked_ms=sent,
                              shell_started_ms=a, host_received_result_ms=received,
                              runner_entered_ms=min(item['start_ms'] for item in executions),
                              runner_finished_ms=right, tool_result_available_ms=b if b >= right else None,
                              run_ids=sorted(ids),
                              statuses=sorted({item['status'] for item in executions if 'status' in item})))
            spans.append((1, 'skill_call_and_return', a if sent is None else sent, b if received is None else received))
        else:
            spans.append((1, 'host_tool', a, b))
    rows = partition(start, end, spans)
    return dict(status='measured' if calls else 'partial', turn=latest['id'], start_source=latest['start_source'],
                runtime_attribution='linked' if calls else 'no_matching_runtime_trace',
                request_received_ms=start, answer_ready_ms=end, response_delivered_ms=None,
                total_ms=end-start, runtime_calls=sorted(calls, key=lambda c:c['runner_entered_ms']), intervals=rows)


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session-log', type=Path)
    parser.add_argument('--turn', help='completed Codex turn ID (default: most recent completed turn)')
    args = parser.parse_args()
    try:
        thread = str(UUID(os.environ['CODEX_THREAD_ID'])) if not args.session_log else str(UUID(args.session_log.stem[-36:]))
        if args.session_log:
            path = args.session_log
        else:
            # Session folders follow local creation dates. Allow UTC/local date boundaries.
            ts = int(thread.replace('-', '')[:12], 16) / 1000
            base = Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex')/'sessions'
            days = {datetime.fromtimestamp(ts, tz).strftime('%Y/%m/%d') for tz in (None, timezone.utc)}
            path, = [p for day in days for p in (base/day).glob('*' + thread + '.jsonl')]
        result = turn_report(path, trace_dir(thread), args.turn)
    except (OSError, KeyError, ValueError, TypeError):
        result = dict(status='unavailable', diagnostic='host_timing_log_unavailable')
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] == 'measured' else 2


if __name__ == '__main__':
    sys.exit(main())
