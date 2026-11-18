"""Opt-in real Claude Code host timing on an isolated synthetic project.

Preparation does not start a model. --run sends one natural-language request
through the ordinary installed CLI, preserving its authentication, settings,
plugins, hooks and permission mode. No personal configuration is written.
The caller owns the synthetic application and closes its recorder afterwards.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
from zipfile import ZipFile


REPO = Path(__file__).resolve().parents[1]
WRAPPER = r'''$ErrorActionPreference = 'Stop'
$entry = [string]$args[0]
$forward = @($args | Select-Object -Skip 1)
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
& $entry @forward
exit $LASTEXITCODE
'''


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def config_hashes():
    """Hash only known settings files. Never read or emit credential material."""
    config = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')
    paths = [config / 'settings.json', config / 'settings.local.json',
             config / 'plugins/installed_plugins.json']
    return {str(p): digest(p) for p in paths if p.is_file()}


def skill_hashes(skill):
    return {str(p.relative_to(skill)).replace('\\', '/'): digest(p)
            for p in sorted(skill.rglob('*')) if p.is_file()
            and '__pycache__' not in p.parts and p.suffix != '.pyc'}


def prepare(root, source):
    if root.exists():
        manifest = root / 'preparation.json'
        if not manifest.is_file():
            raise ValueError('Existing output directory is not a prepared host benchmark.')
        info = json.loads(manifest.read_text(encoding='utf-8'))
        skill = root / 'project/.claude/skills/desktop-select'
        if skill_hashes(skill) != info['skillHashes']:
            raise ValueError('Prepared skill changed; use a fresh output directory.')
        return root / 'project', skill
    source = source.resolve(strict=True)
    root.mkdir(parents=True)
    project = root / 'project'
    skill = project / '.claude/skills/desktop-select'
    skill.parent.mkdir(parents=True)
    if source.is_dir():
        shutil.copytree(source, skill, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.desktop-select', '.git'))
    else:
        with ZipFile(source) as archive:
            for member in archive.infolist():
                target = (skill.parent / member.filename).resolve()
                if not target.is_relative_to(skill.parent.resolve()):
                    raise ValueError('Unsafe archive member.')
            archive.extractall(skill.parent)
    if not (skill / 'SKILL.md').is_file() or not (skill / 'scripts/desktop_select.py').is_file():
        raise ValueError('Desktop Select skill missing from source.')
    (root / 'invoke-claude.ps1').write_text(WRAPPER, encoding='utf-8')
    save(root / 'preparation.json', dict(created=datetime.now(timezone.utc).isoformat(),
         source=str(source), sourceArchiveSha256=digest(source) if source.is_file() else None,
         skillHashes=skill_hashes(skill), settings='normal inherited CLI settings',
         syntheticOnly=True, modelRequestsSent=0))
    return project, skill


def cli_command(root, selected=None):
    root.mkdir(parents=True, exist_ok=True)
    (root / 'invoke-claude.ps1').write_text(WRAPPER, encoding='utf-8')
    entry = selected or shutil.which('claude.exe') or shutil.which('claude.ps1') or shutil.which('claude.cmd') or shutil.which('claude')
    if not entry:
        raise ValueError('Claude Code was not found on PATH.')
    entry = str(Path(entry).resolve(strict=True))
    if Path(entry).suffix.lower() in {'.ps1', '.cmd', '.bat'}:
        shell = shutil.which('pwsh.exe') or shutil.which('powershell.exe')
        if not shell:
            raise ValueError('PowerShell required for the existing Claude shim.')
        return [shell, '-NoLogo', '-NoProfile', '-File', str(root / 'invoke-claude.ps1'), entry]
    return [entry]


def allow_rules(project, skill):
    path = project.as_posix()
    if len(path) > 2 and path[1:3] == ':/':
        path = '//' + path[0].lower() + path[2:]
    # Shell requests are checked against the whole command by the stdio
    # approval handler. A script-prefix wildcard cannot confine its arguments
    # to the named synthetic fixture.
    return ['Skill(desktop-select)', 'Read(' + path + '/**)']


def tool_blocks(event):
    message = event.get('message', {})
    content = message.get('content', []) if isinstance(message, dict) else []
    return content if isinstance(content, list) else []


def receipt_objects(value):
    """Only recover JSON objects from actual tool output, never assistant prose."""
    if isinstance(value, dict):
        if isinstance(value.get('status'), str):
            yield value
        for item in value.values():
            yield from receipt_objects(item)
    elif isinstance(value, list):
        for item in value:
            yield from receipt_objects(item)
    elif isinstance(value, str):
        decoder = json.JSONDecoder()
        for offset, character in enumerate(value):
            if character != '{':
                continue
            try:
                item, _ = decoder.raw_decode(value[offset:])
            except ValueError:
                continue
            if isinstance(item, dict) and isinstance(item.get('status'), str):
                yield item


def summarize(records, recorder_events):
    calls, results, receipts, finals, init, text_times = [], {}, [], [], {}, []
    for record in records:
        event, elapsed = record['event'], record['elapsedSeconds']
        if event.get('type') == 'system' and event.get('subtype') == 'init':
            init = event
        if event.get('type') == 'assistant':
            for block in tool_blocks(event):
                if block.get('type') == 'tool_use':
                    calls.append(dict(id=block.get('id'), tool=block.get('name'),
                                      input=block.get('input'), startedSeconds=elapsed))
                elif block.get('type') == 'text' and str(block.get('text', '')).strip():
                    text_times.append(elapsed)
        if event.get('type') == 'user':
            for block in tool_blocks(event):
                if block.get('type') == 'tool_result':
                    results[block.get('tool_use_id')] = dict(endedSeconds=elapsed, isError=block.get('is_error', False))
                    if block.get('is_error') and 'PreToolUse:' in str(block.get('content', '')):
                        results[block.get('tool_use_id')]['blockedByPreToolHook'] = True
                    for receipt in receipt_objects(block.get('content')):
                        receipts.append(dict(elapsedSeconds=elapsed, receipt=receipt))
        if event.get('type') == 'result':
            finals.append(dict(elapsedSeconds=elapsed, result=event))
    for call in calls:
        if call['id'] in results:
            call.update(results[call['id']])
            call['observedToolSeconds'] = round(call['endedSeconds'] - call['startedSeconds'], 6)
    final = finals[-1] if finals else {}
    result = final.get('result', {})
    ready_file = next((r for r in recorder_events if r['event'].get('status') == 'recorder_ready'), None)
    ready_tool = next((r for r in receipts if r['receipt'].get('status') == 'recorder_ready'), None)
    ready = ready_file or ready_tool
    return dict(model=init.get('model'), permissionMode=init.get('permissionMode'),
                skills=init.get('skills'), plugins=init.get('plugins'),
                initializedSeconds=next((r['elapsedSeconds'] for r in records
                    if r['event'].get('type') == 'system' and r['event'].get('subtype') == 'init'), None),
                toolCalls=calls, receipts=receipts, recorderReadyFile=ready_file, recorderReadyTool=ready_tool,
                firstAssistantTextAfterRecorderReadySeconds=(next((at for at in text_times
                    if at >= ready['elapsedSeconds']), None) if ready else None),
                finalSeconds=final.get('elapsedSeconds'), finalText=result.get('result'),
                resultSubtype=result.get('subtype'), isError=result.get('is_error'),
                durationMs=result.get('duration_ms'), durationApiMs=result.get('duration_api_ms'),
                usage=result.get('usage'), modelUsage=result.get('modelUsage'), costUsd=result.get('total_cost_usd'),
                permissionDenials=result.get('permission_denials'), resultReceived=bool(finals),
                recorderReadyObserved=ready_file is not None or ready_tool is not None)


def run(root, project, skill, args):
    if (root / 'request.json').exists():
        raise ValueError('This prepared run was already used; use a fresh output directory.')
    prompt = args.prompt or f'현재 열린 {args.app} 화면의 작업을 녹화해줘. 녹화창이 준비되면 알려줘. 내가 시작을 눌러 시연할게.'
    command = cli_command(root, args.claude)
    rules = allow_rules(project, skill)
    mode = getattr(args, 'mode', 'recorder')
    approved_commands = set(getattr(args, 'approved_commands', ()))
    validator = getattr(args, 'permission_validator', None)
    if not approved_commands and validator is None and mode == 'recorder':
        # Full-string comparison, not a shell prefix. No extra argument, pipe,
        # statement, expansion or command can be added to this authorization.
        if any(c in args.app for c in '\r\n`$";|&<>'):
            raise ValueError('Use a simple synthetic fixture app label.')
        script = skill / 'scripts/desktop_select.py'
        paths = {str(script), script.as_posix(), '.claude/skills/desktop-select/scripts/desktop_select.py',
                 './.claude/skills/desktop-select/scripts/desktop_select.py',
                 r'.claude\skills\desktop-select\scripts\desktop_select.py',
                 r'.\.claude\skills\desktop-select\scripts\desktop_select.py'}
        for path in paths:
            for executable in ('python', 'python.exe'):
                for quoted in ('"' + path + '"', "'" + path + "'", path):
                    approved_commands.add(f'{executable} -X utf8 -B {quoted} recorder --app "{args.app}"')
    command += ['--print', '--verbose', '--output-format', 'stream-json', '--include-partial-messages',
                '--input-format', 'stream-json', '--permission-prompt-tool', 'stdio',
                '--no-session-persistence', '--permission-prompts', 'host',
                '--max-budget-usd', str(args.budget),
                '--allowedTools', *rules]
    if args.model:
        command += ['--model', args.model]
    command += ['--append-system-prompt',
                'This is an authorized real-host timing test using synthetic local fixtures only. '
                'Use the installed project skills normally. Work only on the named synthetic fixture '
                'and files in the current project. Do not read personal or business data, use external '
                'services or MCP tools, change settings or permissions, or install anything. '
                + ('Stop after opening the recorder and reporting its observed state.' if mode == 'recorder' else
                   'Complete only the requested synthetic workflow and report observed results.')]
    before = config_hashes()
    save(root / 'request.json', dict(prompt=prompt, command=command, project=str(project),
         settings='normal inherited CLI settings; no setting-sources/settings/permission-mode override',
         scopedSessionAllowRules=True, approvedCommands=sorted(approved_commands),
         permissionValidator=bool(validator), permissionMethod='one request at a time; no permission updates',
         inheritedPermissionRulesRemainActive=True, syntheticOnly=True, mode=mode))
    env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    records, observed, seen, decisions = [], [], set(), []
    output = queue.Queue()
    start = time.perf_counter()
    process = subprocess.Popen(command, cwd=project, env=env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace',
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    save(root / 'process.json', dict(pid=process.pid, started=datetime.now(timezone.utc).isoformat(),
                                   project=str(project), command=command))

    def read(stream, kind):
        for line in stream:
            output.put((kind, time.perf_counter() - start, line))
        output.put((kind + '_done', time.perf_counter() - start, ''))

    readers = [threading.Thread(target=read, args=(process.stdout, 'stdout'), daemon=True),
               threading.Thread(target=read, args=(process.stderr, 'stderr'), daemon=True)]
    for reader in readers:
        reader.start()
    def send(value):
        process.stdin.write(json.dumps(value, ensure_ascii=False) + '\n')
        process.stdin.flush()

    send(dict(type='control_request', request_id='host-initialize', request=dict(subtype='initialize')))
    prompt_sent = None
    timed_out, cancelled, closed = False, False, set()
    with (root / 'events.jsonl').open('w', encoding='utf-8') as event_file, \
         (root / 'stderr.txt').open('w', encoding='utf-8') as stderr, \
         (root / 'recorder-events.jsonl').open('w', encoding='utf-8') as recorder_file:
        while len(closed) != 2:
            elapsed = time.perf_counter() - start
            if elapsed > args.timeout + 15:
                timed_out = True
                break
            for path in (project / '.desktop-select/recording-sessions').glob('*/events.jsonl'):
                try:
                    lines = path.read_text(encoding='utf-8').splitlines()
                except (OSError, UnicodeError):
                    continue
                for number, line in enumerate(lines):
                    identity = (str(path), number)
                    if identity in seen:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    seen.add(identity)
                    row = dict(elapsedSeconds=round(elapsed, 6), source=str(path.relative_to(project)), event=event)
                    observed.append(row)
                    recorder_file.write(json.dumps(row, ensure_ascii=False) + '\n')
                    recorder_file.flush()
            if (elapsed > args.timeout or (root / 'stop').exists()) and process.poll() is None:
                cancelled = (root / 'stop').exists()
                timed_out = not cancelled
                if os.name == 'nt':
                    subprocess.run(['taskkill.exe', '/PID', str(process.pid), '/T', '/F'], capture_output=True)
                else:
                    process.kill()
                if process.poll() is None:
                    process.terminate()
            try:
                kind, elapsed, line = output.get(timeout=.05)
            except queue.Empty:
                if timed_out and elapsed > args.timeout + 15:
                    break
                continue
            if kind.endswith('_done'):
                closed.add(kind)
            elif kind == 'stderr':
                stderr.write(line)
                stderr.flush()
            else:
                try:
                    event = json.loads(line)
                except ValueError:
                    event = {'type': 'non_json_output', 'text': line.rstrip()}
                row = dict(elapsedSeconds=round(elapsed, 6), event=event)
                records.append(row)
                event_file.write(json.dumps(row, ensure_ascii=False) + '\n')
                event_file.flush()
                if event.get('type') == 'control_response' and event.get('response', {}).get('request_id') == 'host-initialize':
                    if event['response'].get('subtype') == 'success' and prompt_sent is None:
                        prompt_sent = time.perf_counter() - start
                        send(dict(type='user', message=dict(role='user', content=prompt),
                                  session_id='', parent_tool_use_id=None))
                if event.get('type') == 'control_request':
                    request = event.get('request', {})
                    tool = request.get('tool_name')
                    inputs = request.get('input', {})
                    allow = (request.get('subtype') == 'can_use_tool' and tool in {'Bash', 'PowerShell'}
                             and isinstance(inputs, dict) and inputs.get('command') in approved_commands
                             and set(inputs) <= {'command', 'description', 'timeout', 'run_in_background'})
                    if not allow and request.get('subtype') == 'can_use_tool' and callable(validator):
                        try:
                            allow = validator(request) is True
                        except Exception:
                            # A failed test-owned validator never authorizes a
                            # command and must not strand a waiting CLI child.
                            allow = False
                    decision = dict(elapsedSeconds=round(time.perf_counter()-start, 6),
                                    requestId=event.get('request_id'), tool=tool, allowed=allow,
                                    input=inputs, permissionUpdates=False)
                    decisions.append(decision)
                    save(root / 'permissions.json', decisions)
                    response = (dict(behavior='allow', updatedInput=inputs) if allow else
                                dict(behavior='deny', message='This exact operation is outside the authorized synthetic test. Do not retry it through another tool.'))
                    send(dict(type='control_response', response=dict(subtype='success',
                              request_id=event.get('request_id'), response=response)))
                if event.get('type') in {'assistant', 'result'}:
                    names = [b.get('name') for b in tool_blocks(event) if b.get('type') == 'tool_use']
                    print(json.dumps(dict(elapsedSeconds=row['elapsedSeconds'], type=event.get('type'), tools=names)), flush=True)
                if event.get('type') == 'result' and not process.stdin.closed:
                    process.stdin.close()
    try:
        exit_code = process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        exit_code = None
    after = config_hashes()
    summary = summarize(records, observed)
    if prompt_sent is not None:
        ready = summary['recorderReadyFile'] or summary['recorderReadyTool']
        summary['recorderReadySecondsFromPrompt'] = round(ready['elapsedSeconds'] - prompt_sent, 6) if ready else None
        summary['finalSecondsFromPrompt'] = round(summary['finalSeconds'] - prompt_sent, 6) if summary['finalSeconds'] is not None else None
    summary.update(exitCode=exit_code, timeout=timed_out, cancelled=cancelled, wallSeconds=round(time.perf_counter()-start, 6),
                   cliStartedMonotonic=start,
                   promptSentSeconds=prompt_sent, permissionDecisions=decisions, mode=mode,
                   originalSettingsHashesUnchanged=before == after, beforeSettingsHashes=before,
                   afterSettingsHashes=after, project=str(project), source='real Claude Code CLI',
                   recorderEventPollingIntervalSeconds=.05,
                   caveat='Recorder file time is first poll observation. Tool times are stream event arrival times. '
                          'Existing user hooks/plugins and model network time remain part of this run.')
    save(root / 'summary.json', summary)
    print(json.dumps({k: summary.get(k) for k in ('model', 'exitCode', 'timeout', 'wallSeconds', 'finalSeconds',
                     'resultSubtype', 'originalSettingsHashesUnchanged')}, ensure_ascii=False), flush=True)
    completed = summary['recorderReadyObserved'] if mode == 'recorder' else not summary['isError']
    return 0 if summary['resultReceived'] and completed and not timed_out and exit_code == 0 else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--skill', type=Path, default=REPO / 'skills/desktop-select')
    parser.add_argument('--app', default='FxMenuAgent synthetic fixture')
    parser.add_argument('--prompt')
    parser.add_argument('--claude', help='Existing CLI executable or terminal shim; no installation changes.')
    parser.add_argument('--model', help='Optional explicit session model; default inherits existing selection.')
    parser.add_argument('--timeout', type=float, default=180)
    parser.add_argument('--budget', type=float, default=2)
    parser.add_argument('--mode', choices=('recorder', 'workflow'), default='recorder')
    parser.add_argument('--run', action='store_true', help='Actually send one model request after preparing the project.')
    args = parser.parse_args()
    root = args.root.resolve()
    project, skill = prepare(root, args.skill)
    if not args.run:
        print(json.dumps(dict(status='prepared', project=str(project), modelRequestsSent=0), ensure_ascii=False))
        return 0
    return run(root, project, skill, args)


if __name__ == '__main__':
    raise SystemExit(main())
