import json, os, sys, threading, urllib.error, urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OPT = ROOT / 'rootfs/opt/herdr-runtime'
sys.path.insert(0, str(OPT / 'bridge'))
import herdr_bridge as hb  # noqa: E402

ALICE = 'alice-sub'


def tid(n):
    return 'deleg_' + ('%032x' % n)


def plugin_accepts(data, task_id, generation):
    """The checks the OWUI plugin (herdr_event 1.1.4, Event.poll) applies to GET /tasks/{id}/events."""
    assert data.get('state') in ('running', 'blocked', 'completed', 'failed', 'interrupted')
    events = data.get('events')
    assert isinstance(events, list) and len(events) <= 64
    seen = {}
    for e in events:
        assert e.get('task_id') == task_id and e.get('generation') == generation
        assert e.get('kind') in ('needs_input', 'completed', 'failed', 'interrupted')
        assert type(e.get('seq')) is int and e['seq'] >= 1
        assert isinstance(e.get('event_id'), str) and 0 < len(e['event_id']) <= 128
        assert len(json.dumps(e)) <= 20000
        if e['kind'] == 'needs_input':
            q = e.get('question')
            assert isinstance(q, dict) and isinstance(q.get('id'), str) and 0 < len(q['id']) <= 128
            assert type(q.get('version')) is int and q['version'] >= 1
            assert isinstance(q.get('text'), str) and len(q['text']) <= 2000
        assert seen.setdefault(e['event_id'], e) == e
    return events


class FakeHerdr:
    def __init__(self):
        self.calls, self.prompts, self.agents = [], [], {}
        self.fail, self.react = None, True

    def snapshot(self):
        return {}

    def workspace(self, label, cwd):
        self.calls.append(('workspace', label, str(cwd)))
        return 'w1'

    def create_tab(self, ws, label, cwd):
        self.calls.append(('tab', ws, label, str(cwd)))
        if self.fail == 'tab':
            raise hb.HerdrError('tab_failed')
        return 'w1:t2', 'w1:p2'

    def start_agent(self, name, kind, pane, args):
        self.calls.append(('start', name, kind, pane, list(args)))
        if self.fail == 'start':
            raise hb.HerdrError('agent_not_ready')
        self.agents[name] = {'agent_status': 'idle', 'terminal_id': 'term_' + name, 'state_change_seq': 1}

    def agent(self, name):
        return self.agents.get(name)

    def prompt(self, name, text):
        self.prompts.append((name, text))
        a = self.agents.get(name)
        if a and self.react:
            a.update(agent_status='working', state_change_seq=a['state_change_seq'] + 1)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        self.t += 1
        return self.t


def make(tmp_path, herdr=None, **env):
    base = {'HOME': str(tmp_path), 'ALLOWED_SUBS': ALICE + ',bob-sub', 'LEAD_MODEL_CLAUDE': 'opus',
            'TASK_TEMPLATE': str(OPT / 'templates/lead-task.md'), 'BRIDGE_PORT': '0', 'BRIDGE_INTERNAL_PORT': '0'}
    base.update(env)
    return hb.Bridge(hb.Config(base), herdr or FakeHerdr(), clock=Clock(), sleep=lambda s: None,
                     spawn=lambda fn, *a: fn(*a))


def start(b, n=1, owner=ALICE, task='修一个 README 错字'):
    code, body = b.create(owner, {'id': tid(n), 'generation': 1, 'request_key': 'key%d' % n, 'task': task})
    assert code == 200, body
    return tid(n)


def report(b, task_id, text):
    Path(b.task(task_id)['task_dir'], 'REPORT.md').write_text(text)


def events(b, task_id, owner=ALICE):
    code, data = b.events(owner, task_id)
    assert code == 200
    return data, plugin_accepts(data, task_id, 1)


# ---- identity over real HTTP ----

def test_identity_only_trusts_allow_listed_router_sub(tmp_path):
    b = make(tmp_path)
    server = hb.serve(b)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = 'http://127.0.0.1:%d' % server.server_address[1]

    def status(path, sub=None):
        req = urllib.request.Request(base + path, headers={hb.SUB_HEADER: sub} if sub else {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
    try:
        assert status('/healthz') == 200
        assert status('/tasks') == 403
        assert status('/tasks', 'mallory') == 403
        assert status('/tasks', ALICE) == 200
        assert status('/tasks/%s/events' % tid(9), ALICE) == 404
    finally:
        server.shutdown()


# ---- create ----

def test_create_is_idempotent_and_rejects_a_changed_request(tmp_path):
    b = make(tmp_path)
    body = {'id': tid(1), 'generation': 1, 'request_key': 'k', 'task': 'x'}
    assert b.create(ALICE, body) == (200, {'id': tid(1), 'status': 'started'})
    assert b.create(ALICE, body) == (200, {'id': tid(1), 'status': 'existing'})
    assert b.create(ALICE, {**body, 'request_key': 'other'})[0] == 409
    assert b.create('bob-sub', body)[0] == 404
    assert len([c for c in b.herdr.calls if c[0] == 'start']) == 1


def test_create_validates_input_and_capacity(tmp_path):
    b = make(tmp_path)
    assert b.create(ALICE, {'id': 'deleg_xyz', 'task': 'x'})[0] == 400
    assert b.create(ALICE, {'id': tid(1), 'task': ''})[0] == 400
    assert b.create(ALICE, {'id': tid(1), 'task': 'x' * 8001})[0] == 400
    assert b.create(ALICE, {'id': tid(1), 'task': 'x', 'generation': 0})[0] == 400
    start(b, 1)
    assert b.create(ALICE, {'id': tid(2), 'generation': 1, 'request_key': 'k2', 'task': 'y'})[0] == 429
    assert b.create('bob-sub', {'id': tid(3), 'generation': 1, 'request_key': 'k3', 'task': 'y'})[0] == 200


# ---- launch ----

def test_launch_starts_an_allow_listed_yolo_lead_with_a_pinned_model(tmp_path):
    b = make(tmp_path)
    t = start(b, 1, task='改 {{TASK_DIR}} 这个字面量')
    task_dir = tmp_path / 'workspaces/assistant/tasks' / t
    assert ('tab', 'w1', 'T·' + t[6:14], str(task_dir)) in b.herdr.calls
    assert ('start', 'lead-' + t[6:], 'claude', 'w1:p2', ['--dangerously-skip-permissions', '--model', 'opus']) \
        in b.herdr.calls
    brief = (task_dir / 'TASK.md').read_text()
    assert '改 {{TASK_DIR}} 这个字面量' in brief          # user text is inserted verbatim, never expanded
    assert str(task_dir / 'REPORT.md') in brief
    assert b.herdr.prompts == [('lead-' + t[6:], '读 %s 并照做。' % (task_dir / 'TASK.md'))]
    data, evs = events(b, t)
    assert data['state'] == 'running' and evs == []


@pytest.mark.parametrize('task', ['@profile codex\n做点事', '@profile gemini\n做点事'])
def test_profile_without_model_or_outside_allow_list_fails_without_starting(tmp_path, task):
    b = make(tmp_path)
    t = start(b, 1, task=task)
    data, evs = events(b, t)
    assert data['state'] == 'failed' and [e['kind'] for e in evs] == ['failed']
    assert not [c for c in b.herdr.calls if c[0] == 'start']


def test_codex_profile_uses_its_own_yolo_flag(tmp_path):
    b = make(tmp_path, LEAD_MODEL_CODEX='gpt-x')
    t = start(b, 1, task='@profile codex\n做点事')
    assert ('start', 'lead-' + t[6:], 'codex', 'w1:p2',
            ['--dangerously-bypass-approvals-and-sandbox', '--model', 'gpt-x']) in b.herdr.calls
    assert b.task(t)['text'] == '做点事'


def test_launch_failure_is_reported_once_and_not_retried(tmp_path):
    h = FakeHerdr()
    h.fail = 'start'
    b = make(tmp_path, h)
    t = start(b, 1)
    b.observe_all()
    data, evs = events(b, t)
    assert data['state'] == 'failed' and len(evs) == 1 and 'agent_start' in evs[0]['summary']
    assert len([c for c in h.calls if c[0] == 'start']) == 1


def test_swallowed_prompt_asks_for_a_takeover_instead_of_retyping(tmp_path):
    h = FakeHerdr()
    h.react = False   # e.g. the CLI still shows its first-run theme picker
    b = make(tmp_path, h)
    t = start(b, 1)
    data, evs = events(b, t)
    assert data['state'] == 'blocked' and evs[-1]['kind'] == 'needs_input'
    assert '接管' in data['question']['text']
    assert len(h.prompts) == 1


# ---- observation ----

def test_report_done_completes_once(tmp_path):
    b = make(tmp_path)
    t = start(b, 1)
    report(b, t, 'STATUS: in-progress\n')
    b.observe_all()
    assert events(b, t)[1] == []
    report(b, t, 'STATUS: done\nPR: https://example.invalid/pr/1\n')
    b.observe_all()
    b.observe_all()
    data, evs = events(b, t)
    assert data['state'] == 'completed' and [e['kind'] for e in evs] == ['completed']
    assert 'PR: https://example.invalid/pr/1' in evs[0]['summary']


def test_report_symlink_is_not_followed(tmp_path):
    b = make(tmp_path)
    t = start(b, 1)
    target = tmp_path / 'elsewhere.md'
    target.write_text('STATUS: done\nsecret\n')
    os.symlink(target, Path(b.task(t)['task_dir'], 'REPORT.md'))
    b.observe_all()
    assert events(b, t)[0]['state'] == 'running'


def test_blocked_report_round_trip_with_receipts_and_stale_answers(tmp_path):
    b = make(tmp_path)
    t = start(b, 1)
    name = 'lead-' + t[6:]
    report(b, t, 'STATUS: blocked（需要用户决定：用 A 还是 B）\n')
    b.observe_all()
    data, evs = events(b, t)
    assert data['state'] == 'blocked' and data['question']['id'] == 'q1' and data['question']['version'] == 1
    assert '用 A 还是 B' in data['question']['text'] and evs[-1]['kind'] == 'needs_input'

    reply = {'question_id': 'q1', 'version': 1, 'answer': 'B', 'request_key': 'r1'}
    assert b.reply(ALICE, t, {**reply, 'version': 2})[0] == 409
    assert b.reply('bob-sub', t, reply)[0] == 404
    assert b.reply(ALICE, t, reply) == (200, {'status': 'answered', 'question_id': 'q1', 'version': 1})
    assert b.reply(ALICE, t, reply)[1]['status'] == 'already_answered'       # lost-response retry is safe
    assert b.reply(ALICE, t, {**reply, 'request_key': 'r2', 'answer': 'A'})[0] == 409
    answer = Path(b.task(t)['task_dir'], 'answers/q1.md')
    assert 'B' in answer.read_text()
    assert b.herdr.prompts[-1][0] == name and str(answer) in b.herdr.prompts[-1][1]
    assert events(b, t)[0]['state'] == 'running'

    b.observe_all()                                    # the same blocked report is not asked again
    assert len(events(b, t)[1]) == 1
    report(b, t, 'STATUS: blocked（需要用户决定：第二个问题）\n')
    b.observe_all()
    data, evs = events(b, t)
    assert data['question']['id'] == 'q2' and data['question']['version'] == 2 and len(evs) == 2


def test_task_ask_registers_and_receives_the_answer(tmp_path):
    b = make(tmp_path)
    t = start(b, 1)
    assert b.internal_ask({'task_id': t, 'text': '要不要合并？'}) == (200, {'question_id': 'q1', 'version': 1})
    assert events(b, t)[0]['state'] == 'blocked'
    assert b.internal_question(t, 'q1')[1]['state'] == 'open'
    b.reply(ALICE, t, {'question_id': 'q1', 'version': 1, 'answer': '先不合并', 'request_key': 'r'})
    assert b.internal_question(t, 'q1')[1] == {'question_id': 'q1', 'version': 1, 'state': 'answered',
                                               'answer': '先不合并'}
    assert b.internal_ask({'task_id': tid(99), 'text': 'x'})[0] == 409


def test_vanished_lead_fails_after_several_polls(tmp_path):
    b = make(tmp_path)
    t = start(b, 1)
    del b.herdr.agents['lead-' + t[6:]]
    for _ in range(hb.MISSING_POLLS - 1):
        b.observe_all()
    assert events(b, t)[0]['state'] == 'running'
    b.observe_all()
    data, evs = events(b, t)
    assert data['state'] == 'failed' and [e['kind'] for e in evs] == ['failed']


def test_cli_prompt_in_the_terminal_asks_for_a_takeover_once(tmp_path):
    b = make(tmp_path)
    t = start(b, 1)
    b.herdr.agents['lead-' + t[6:]]['agent_status'] = 'blocked'
    for _ in range(hb.TUI_POLLS + 3):
        b.observe_all()
    data, evs = events(b, t)
    assert data['state'] == 'blocked' and [e['kind'] for e in evs] == ['needs_input']


def test_restart_adopts_the_same_terminal_and_interrupts_the_rest(tmp_path):
    h = FakeHerdr()
    b = make(tmp_path, h, MAX_ACTIVE_TASKS='5')
    lost, kept, asked = start(b, 1), start(b, 2), start(b, 3)
    b.internal_ask({'task_id': asked, 'text': '?'})
    del h.agents['lead-' + lost[6:]]
    h.agents['lead-' + asked[6:]]['terminal_id'] = 'term_new'      # same name, different terminal = new instance
    b2 = make(tmp_path, h, MAX_ACTIVE_TASKS='5')
    b2.recover()
    assert events(b2, lost)[0]['state'] == 'interrupted'
    assert events(b2, kept)[0]['state'] == 'running'
    data, evs = events(b2, asked)
    assert data['state'] == 'interrupted' and data['question'] is None
    assert [e['kind'] for e in evs] == ['needs_input', 'interrupted']
    assert b2.reply(ALICE, asked, {'question_id': 'q1', 'version': 1, 'answer': 'x', 'request_key': 'late'})[0] == 409
    assert len([c for c in h.calls if c[0] == 'start']) == 3          # nothing was re-run


def test_event_count_never_exceeds_the_plugin_limit(tmp_path):
    b = make(tmp_path)
    t = start(b, 1)
    for i in range(hb.MAX_QUESTIONS + 5):
        b.internal_ask({'task_id': t, 'text': 'q%d' % i})
    report(b, t, 'STATUS: done\n')
    b.observe_all()
    data, evs = events(b, t)
    assert len(evs) <= hb.EVENT_LIMIT and data['state'] == 'completed'
