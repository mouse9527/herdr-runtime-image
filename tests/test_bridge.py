import json, os, sqlite3, sys, threading, urllib.error, urllib.request
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
        self.calls, self.prompts, self.agents, self.closed = [], [], {}, []
        self.fail, self.react, self.panes = None, True, 0

    def snapshot(self):
        return {}

    def workspace(self, label, cwd):
        return 'w1'

    def create_tab(self, ws, label, cwd):
        self.calls.append(('tab', label, str(cwd)))
        if self.fail == 'tab':
            raise hb.HerdrError('tab_failed')
        self.panes += 1
        return 'w1:t%d' % self.panes, 'w1:p%d' % self.panes

    def start_agent(self, name, kind, pane, args):
        self.calls.append(('start', name, kind, pane, list(args)))
        if self.fail == 'start':
            raise hb.HerdrError('agent_not_ready')
        self.agents[name] = {'agent_status': 'idle', 'terminal_id': 'term_' + name, 'state_change_seq': 1}

    def agent(self, name):
        return self.agents.get(name)

    def prompt(self, name, text):
        a = self.agents.get(name)
        if a and a['agent_status'] == 'blocked':
            raise hb.HerdrError('agent_blocked')
        self.prompts.append((name, text))
        if a and self.react:
            a.update(agent_status='working', state_change_seq=a['state_change_seq'] + 1)

    def close_pane(self, pane):
        self.closed.append(pane)
        for name, a in list(self.agents.items()):
            if a.get('pane') == pane or name in self.pane_owner(pane):
                del self.agents[name]

    def pane_owner(self, pane):
        return [c[1] for c in self.calls if c[0] == 'start' and c[3] == pane]

    def starts(self):
        return [c for c in self.calls if c[0] == 'start']


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        self.t += 1
        return self.t


def make(tmp_path, herdr=None, **env):
    base = {'HOME': str(tmp_path), 'ALLOWED_SUBS': ALICE + ',bob-sub', 'LEAD_MODEL_CLAUDE': 'opus',
            'TEMPLATE_DIR': str(OPT / 'templates'), 'BRIDGE_PORT': '0', 'BRIDGE_INTERNAL_PORT': '0'}
    base.update(env)
    return hb.Bridge(hb.Config(base), herdr or FakeHerdr(), clock=Clock(), sleep=lambda s: None,
                     spawn=lambda fn, *a: fn(*a))


def start(b, n, owner=ALICE, task='修一个 README 错字', **extra):
    code, body = b.create(owner, {'id': tid(n), 'generation': 1, 'request_key': 'key%d' % n, 'task': task, **extra})
    assert code == 200, body
    return tid(n)


def report(b, task_id, text):
    Path(b.task(task_id)['task_dir'], 'REPORT.md').write_text(text)


def events(b, task_id, owner=ALICE):
    code, data = b.events(owner, task_id)
    assert code == 200
    return data, plugin_accepts(data, task_id, 1)


def topic_of(b, task_id):
    return b.topic(b.task(task_id)['topic_id'])


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
        assert status('/topics', 'mallory') == 403
        assert status('/tasks', ALICE) == 200
        assert status('/topics', ALICE) == 200
        assert status('/tasks/%s/events' % tid(9), ALICE) == 404
    finally:
        server.shutdown()


# ---- create ----

def test_create_is_idempotent_and_rejects_a_changed_request(tmp_path):
    b = make(tmp_path)
    body = {'id': tid(1), 'generation': 1, 'request_key': 'k', 'task': 'x'}
    code, first = b.create(ALICE, body)
    assert code == 200 and first['status'] == 'started' and first['topic']['id'].startswith('topic_')
    assert b.create(ALICE, body) == (200, {'id': tid(1), 'status': 'existing', 'topic': first['topic']})
    assert b.create(ALICE, {**body, 'request_key': 'other'})[0] == 409
    assert b.create('bob-sub', body)[0] == 404
    assert len(b.herdr.starts()) == 1


def test_create_validates_input_and_sets_no_limits(tmp_path):
    b = make(tmp_path)
    assert b.create(ALICE, {'id': 'deleg_xyz', 'task': 'x'})[0] == 400
    assert b.create(ALICE, {'id': tid(1), 'task': ''})[0] == 400
    assert b.create(ALICE, {'id': tid(1), 'task': 'x' * 8001})[0] == 400
    assert b.create(ALICE, {'id': tid(1), 'task': 'x', 'generation': 0})[0] == 400
    assert b.create(ALICE, {'id': tid(1), 'task': 'x', 'topic': ''})[0] == 400
    for n in range(1, 13):          # no per-user, per-topic or topic-count limit
        start(b, n, parent_chat_id='chat-%d' % (n % 6))
    assert len(b.list_topics(ALICE)[1]['topics']) == 6


# ---- topics ----

def test_first_task_starts_a_topic_lead_with_yolo_flags_and_pinned_model(tmp_path):
    b = make(tmp_path)
    t = start(b, 1, task='改 {{TASK_DIR}} 这个字面量', parent_chat_id='chat-a')
    tp = topic_of(b, t)
    topic_dir = tmp_path / 'workspaces/assistant/topics' / tp['id']
    task_dir = topic_dir / 'tasks' / t
    name = 'lead-%s-1' % tp['id'][6:]
    assert b.herdr.starts() == [('start', name, 'claude', 'w1:p1',
                                 ['--dangerously-skip-permissions', '--model', 'opus'])]
    assert ('tab', 'T·' + tp['name'][:24], str(topic_dir)) in b.herdr.calls
    assert '改 {{TASK_DIR}} 这个字面量' in (task_dir / 'TASK.md').read_text()   # user text never expanded
    assert str(topic_dir / 'NOTES.md') in (topic_dir / 'TOPIC.md').read_text()
    assert b.herdr.prompts == [(name, '读 %s/TOPIC.md，然后读 %s 并照做。' % (topic_dir, task_dir / 'TASK.md'))]
    data, evs = events(b, t)
    assert data['state'] == 'running' and evs == [] and data['topic']['id'] == tp['id']


def test_same_chat_reuses_the_lead_and_concurrent_tasks_finish_independently(tmp_path):
    b = make(tmp_path)
    ids = [start(b, n, parent_chat_id='chat-a') for n in (1, 2, 3)]
    assert len({b.task(t)['topic_id'] for t in ids}) == 1
    assert len(b.herdr.starts()) == 1
    assert len(b.herdr.prompts) == 3 and all('新任务' in p for _, p in b.herdr.prompts[1:])
    # the lead stays 'working' the whole time; only each task's own REPORT.md decides
    report(b, ids[1], 'STATUS: done\n第二个做完\n')
    b.observe_all()
    assert [events(b, t)[0]['state'] for t in ids] == ['running', 'completed', 'running']
    report(b, ids[0], 'STATUS: done\n')
    report(b, ids[2], 'STATUS: blocked（需要用户决定：选哪个）\n')
    b.observe_all()
    assert [events(b, t)[0]['state'] for t in ids] == ['completed', 'completed', 'blocked']
    assert '[topic ' in events(b, ids[1])[1][0]['summary']


def test_topic_routing_by_name_by_chat_and_backward_compatible_requests(tmp_path):
    b = make(tmp_path)
    a1 = start(b, 1, task='做 A', parent_chat_id='chat-a', topic='发布流程')
    b1 = start(b, 2, task='做 B', parent_chat_id='chat-b')
    assert topic_of(b, a1)['name'] == '发布流程' and topic_of(b, b1)['id'] != topic_of(b, a1)['id']
    x = start(b, 3, parent_chat_id='chat-b', topic='发布流程')          # named explicitly from another chat
    assert topic_of(b, x)['id'] == topic_of(b, a1)['id']
    y = start(b, 4, parent_chat_id='chat-b')                           # that chat now defaults to the last used topic
    assert topic_of(b, y)['id'] == topic_of(b, a1)['id']
    z = start(b, 5, parent_chat_id='chat-b', topic='另一件事')           # "start another one"
    assert topic_of(b, z)['id'] not in (topic_of(b, a1)['id'], topic_of(b, b1)['id'])
    by_id = start(b, 6, topic=topic_of(b, b1)['id'])
    assert topic_of(b, by_id)['id'] == topic_of(b, b1)['id']
    old1, old2 = start(b, 7), start(b, 8)                              # plugin 1.1.4: no topic, no chat
    assert topic_of(b, old1)['id'] != topic_of(b, old2)['id']
    assert start(b, 9, owner='bob-sub', topic='发布流程') and \
        topic_of(b, tid(9))['id'] != topic_of(b, a1)['id']              # topics never cross users


@pytest.mark.parametrize('task', ['@profile codex\n做点事', '@profile gemini\n做点事'])
def test_profile_without_model_or_outside_allow_list_fails_without_starting(tmp_path, task):
    b = make(tmp_path)
    t = start(b, 1, task=task)
    data, evs = events(b, t)
    assert data['state'] == 'failed' and [e['kind'] for e in evs] == ['failed']
    assert not b.herdr.starts()


def test_codex_profile_uses_its_own_yolo_flag(tmp_path):
    b = make(tmp_path, LEAD_MODEL_CODEX='gpt-x')
    t = start(b, 1, task='@profile codex\n做点事')
    assert b.herdr.starts()[0][2:] == ('codex', 'w1:p1', ['--dangerously-bypass-approvals-and-sandbox', '--model', 'gpt-x'])
    assert b.task(t)['text'] == '做点事'


def test_launch_failure_is_reported_once_and_not_retried(tmp_path):
    h = FakeHerdr()
    h.fail = 'start'
    b = make(tmp_path, h)
    t = start(b, 1)
    b.observe_all()
    data, evs = events(b, t)
    assert data['state'] == 'failed' and len(evs) == 1 and 'lead' in evs[0]['summary']
    assert len(h.starts()) == 1


def test_swallowed_first_prompt_asks_for_a_takeover_then_resends_the_brief(tmp_path):
    h = FakeHerdr()
    h.react = False   # e.g. the CLI still shows its first-run theme picker
    b = make(tmp_path, h)
    t = start(b, 1)
    data, evs = events(b, t)
    assert data['state'] == 'blocked' and evs[-1]['kind'] == 'needs_input' and '接管' in data['question']['text']
    assert len(h.prompts) == 1
    b.reply(ALICE, t, {'question_id': 'q1', 'version': 1, 'answer': '处理好了', 'request_key': 'r'})
    assert len(h.prompts) == 2 and 'TOPIC.md' in h.prompts[-1][1] and t in h.prompts[-1][1]


def test_new_task_for_a_blocked_lead_waits_for_a_takeover(tmp_path):
    b = make(tmp_path)
    first = start(b, 1, parent_chat_id='c')
    name = topic_of(b, first)['agent_name']
    b.herdr.agents[name]['agent_status'] = 'blocked'
    t = start(b, 2, parent_chat_id='c')
    data, _ = events(b, t)
    assert data['state'] == 'blocked' and '接管' in data['question']['text']
    assert not b.task(t)['prompted'] and len(b.herdr.prompts) == 1
    b.herdr.agents[name]['agent_status'] = 'idle'
    b.reply(ALICE, t, {'question_id': 'q1', 'version': 1, 'answer': 'ok', 'request_key': 'r'})
    assert b.task(t)['prompted'] and t in b.herdr.prompts[-1][1]


# ---- observation ----

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
    name = topic_of(b, t)['agent_name']
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
    assert b.herdr.prompts[-1][0] == name and str(answer) in b.herdr.prompts[-1][1] and t in b.herdr.prompts[-1][1]
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
    b.reply(ALICE, t, {'question_id': 'q1', 'version': 1, 'answer': '先不合并', 'request_key': 'r'})
    assert b.internal_question(t, 'q1')[1] == {'question_id': 'q1', 'version': 1, 'state': 'answered',
                                               'answer': '先不合并'}
    assert b.internal_ask({'task_id': tid(99), 'text': 'x'})[0] == 409


def test_vanished_lead_fails_its_tasks_and_the_next_task_gets_a_new_lead(tmp_path):
    b = make(tmp_path)
    t1, t2 = start(b, 1, parent_chat_id='c'), start(b, 2, parent_chat_id='c')
    tp = topic_of(b, t1)
    del b.herdr.agents[tp['agent_name']]
    for _ in range(hb.MISSING_POLLS - 1):
        b.observe_all()
    assert events(b, t1)[0]['state'] == 'running'
    b.observe_all()
    assert [events(b, t)[0]['state'] for t in (t1, t2)] == ['failed', 'failed']
    t3 = start(b, 3, parent_chat_id='c')
    assert topic_of(b, t3)['id'] == tp['id'] and b.herdr.starts()[-1][1] == 'lead-%s-2' % tp['id'][6:]


def test_cli_dialog_in_the_terminal_asks_for_a_takeover_once(tmp_path):
    b = make(tmp_path)
    t = start(b, 1)
    b.herdr.agents[topic_of(b, t)['agent_name']]['agent_status'] = 'blocked'
    for _ in range(hb.TUI_POLLS + 3):
        b.observe_all()
    data, evs = events(b, t)
    assert data['state'] == 'blocked' and [e['kind'] for e in evs] == ['needs_input']


def test_restart_adopts_the_same_terminal_and_interrupts_the_rest(tmp_path):
    h = FakeHerdr()
    b = make(tmp_path, h)
    lost, kept, asked = start(b, 1, topic='a'), start(b, 2, topic='b'), start(b, 3, topic='c')
    b.internal_ask({'task_id': kept, 'text': '?'})
    del h.agents[topic_of(b, lost)['agent_name']]
    h.agents[topic_of(b, asked)['agent_name']]['terminal_id'] = 'term_new'   # same name, different terminal
    b2 = make(tmp_path, h)
    b2.recover()
    assert events(b2, lost)[0]['state'] == 'interrupted'
    data, _ = events(b2, kept)
    assert data['state'] == 'blocked' and data['question']['id'] == 'q1'      # adopted: question still open
    data, evs = events(b2, asked)
    assert data['state'] == 'interrupted' and data['question'] is None
    assert b2.reply(ALICE, asked, {'question_id': 'x', 'version': 1, 'answer': 'x', 'request_key': 'late'})[0] == 409
    assert len(h.starts()) == 3                                               # nothing was re-run


def test_idle_topic_writes_notes_then_closes_and_a_new_lead_reads_them(tmp_path):
    b = make(tmp_path, TOPIC_IDLE_DAYS='0')
    t = start(b, 1, parent_chat_id='c')
    report(b, t, 'STATUS: done\n')
    b.observe_all()                      # completes the task
    b.observe_all()                      # topic idle -> closing
    tp = topic_of(b, t)
    assert tp['state'] == 'closing' and 'NOTES.md' in b.herdr.prompts[-1][1]
    b.observe_all()
    assert topic_of(b, t)['state'] == 'closing'          # waits for NOTES.md
    Path(tp['dir'], 'NOTES.md').write_text('结论：……\n')
    os.utime(Path(tp['dir'], 'NOTES.md'), (b.clock() + 10, b.clock() + 10))
    b.observe_all()
    tp = topic_of(b, t)
    assert tp['state'] == 'closed' and tp['agent_name'] is None and b.herdr.closed == ['w1:p1']
    t2 = start(b, 2, parent_chat_id='c')
    assert topic_of(b, t2)['state'] == 'open' and len(b.herdr.starts()) == 2
    assert 'TOPIC.md' in b.herdr.prompts[-1][1]


def test_explicit_close_refuses_while_tasks_are_in_flight(tmp_path):
    b = make(tmp_path)
    t = start(b, 1)
    topic_id = b.task(t)['topic_id']
    assert b.close_topic(ALICE, topic_id) == (409, {'error': 'active_tasks'})
    assert b.close_topic('bob-sub', topic_id)[0] == 404
    report(b, t, 'STATUS: done\n')
    b.observe_all()
    assert b.close_topic(ALICE, topic_id) == (200, {'id': topic_id, 'state': 'closing'})


def test_event_count_never_exceeds_the_plugin_limit(tmp_path):
    b = make(tmp_path)
    t = start(b, 1)
    for i in range(hb.MAX_QUESTIONS + 5):
        b.internal_ask({'task_id': t, 'text': 'q%d' % i})
    report(b, t, 'STATUS: done\n')
    b.observe_all()
    data, evs = events(b, t)
    assert len(evs) <= hb.EVENT_LIMIT and data['state'] == 'completed'


# ---- herdr CLI wrapper and ledger storage ----

def test_herdr_errors_on_stderr_are_understood(tmp_path):
    fake = tmp_path / 'herdr'
    fake.write_text('#!/bin/sh\necho \'{"error":{"code":"agent_not_found","message":"x"},"id":"cli"}\' >&2\nexit 1\n')
    fake.chmod(0o755)
    assert hb.Herdr('assistant', binary=str(fake)).agent('nobody') is None


def test_ledger_uses_a_rollback_journal_not_wal(tmp_path):
    b = make(tmp_path)
    start(b, 1)
    path = b.cfg.state_dir / 'ledger.sqlite3'
    assert sqlite3.connect(str(path)).execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
    assert not Path(str(path) + '-wal').exists()
