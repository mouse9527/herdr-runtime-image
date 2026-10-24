"""herdr task bridge: hands delegated tasks to topic leads in herdr and reports each task's progress.

REST API on :8080 (client: the Open WebUI event plugin herdr_event; 1.1.4 requests still work unchanged):
  POST /tasks                  {id, generation, request_key, task, topic?, parent_chat_id?}  create or return the same
  GET  /tasks/{id}/events      {state, question, events[<=64], topic}
  POST /tasks/{id}/reply       {question_id, version, answer, request_key}   receipt first, then 409 when stale
  GET  /tasks                  the caller's tasks
  GET  /topics                 the caller's topics
  POST /topics/{id}/close      ask the topic's lead to write NOTES.md, then close it
  GET  /tasks/{id}/files       {files[<=500]: {path, type, size?}, truncated}  the task directory, symlinks not followed
  POST /tasks/{id}/read        {path?='REPORT.md'} -> {path, size, truncated, binary, content}  first 64 KB of one file
                               (read-only; the path is in the body because the router admits only [A-Za-z0-9_:/-] paths)
  GET  /healthz /readyz
  POST /mcp                    the same API as one MCP tool (k8s#64 AC6, below)
MCP (Streamable HTTP, JSON responses, stateless): tool `bridge_request` {method: GET|POST, path, body?} runs exactly
the REST request above and returns {status, body}. The OWUI plugins reach it through the LiteLLM MCP gateway with the
user's own LiteLLM identity (the gateway signs the caller; the router verifies it and sets the header below), so no
client has to sign identities itself. Same identity rule as REST.
Identity: the only trusted identity is the `x-mcp-router-sub` header set by the router after it verified the user's
signed token; it must be in ALLOWED_SUBS. Safe only while NetworkPolicy lets nothing but that router reach the port.

Internal endpoint for the `task-ask` helper, 127.0.0.1:9100 only (never in a Service):
  POST /internal/ask                       {task_id, text}  register a question for the human
  GET  /internal/questions/{task}/{qid}    {state, answer}

Topics: a topic is one long-lived lead (one herdr tab) plus ~/workspaces/assistant/topics/<topic_id>/. A task joins
the topic named in the request, else the topic last used from the same chat, else a new topic. Every task gets
topics/<topic>/tasks/<id>/TASK.md and is prompted to the topic's lead, even while the lead is busy with other tasks:
there is no queue and no limit, the lead schedules its own work and workers. Each task's state comes only from its
own REPORT.md first line (`STATUS: in-progress | done | blocked（...）`), never from the lead's herdr state. Events:
needs_input / completed / failed / interrupted. Idle or explicitly closed topics get NOTES.md written by their lead and
the pane closed; the next task in that topic starts a new lead that reads NOTES.md first.

Restart: a lead is adopted only if the same herdr terminal is still there; otherwise its in-flight tasks are reported
interrupted and never re-run automatically.

Trust boundary: the bridge runs as the same UID as the agents it starts. It is a convenience layer for one user, not
an authorization gate: an agent can read or modify this ledger.
"""
import codecs, hashlib, json, os, re, sqlite3, stat, subprocess, sys, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SUB_HEADER = 'x-mcp-router-sub'
TASK_ID = re.compile(r'deleg_[0-9a-f]{32}')
TOPIC_ID = re.compile(r'topic_[0-9a-f]{12}')
# Profile allow-list: herdr agent kind -> CLI flags that start it without approval prompts (Owner 2026-10-22).
PROFILES = {
    'claude': ['--dangerously-skip-permissions'],
    'codex': ['--dangerously-bypass-approvals-and-sandbox'],
    'pi': [],
}
ACTIVE = ('accepted', 'running', 'blocked')
EVENT_LIMIT = 64
MAX_QUESTIONS = 16
STATUS_LINE = re.compile(r'^\s*STATUS:\s*(in-progress|done|blocked)\b\s*(.*)$')
PROFILE_LINE = re.compile(r'^@profile\s+([a-z]+)\s*$')
MISSING_POLLS = 3            # consecutive polls without the lead before its in-flight tasks fail
TUI_POLLS = 3                # consecutive polls of herdr 'blocked' before asking for a takeover
PROMPT_CONFIRM_SECONDS = 30  # a new lead must visibly start working this long after its first prompt
CLOSE_WAIT_SECONDS = 1800    # how long a closing lead gets to write NOTES.md
FILE_LIMIT = 65536           # bytes POST /tasks/{id}/read returns; a larger file is cut and marked truncated
LIST_LIMIT = 500             # entries GET /tasks/{id}/files returns
LIST_DEPTH = 6               # directory levels GET /tasks/{id}/files descends
# Completion event: REPORT.md after its first line, up to this many characters (was 1500). The event plugin rejects an
# event whose json.dumps (ASCII-escaped: 6 bytes per CJK character) exceeds 20000; 2500 CJK characters plus the path
# and topic name stay under it. The full report is one herdr_read (POST /tasks/{id}/read) away.
SUMMARY_CHARS = 2500


def log(*a):
    print(time.strftime('%Y-%m-%dT%H:%M:%S'), *a, flush=True)


class Config:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        home = Path(env.get('HOME') or '/home/agent')
        self.allowed = {x.strip() for x in env.get('ALLOWED_SUBS', '').split(',') if x.strip()}
        self.host = env.get('BRIDGE_HOST', '0.0.0.0')
        self.port = int(env.get('BRIDGE_PORT', '8080'))
        self.internal_port = int(env.get('BRIDGE_INTERNAL_PORT', '9100'))
        self.state_dir = Path(env.get('BRIDGE_STATE_DIR') or home / '.local/state/herdr-bridge')
        self.ws_root = Path(env.get('WS_ROOT') or home / 'workspaces/assistant')
        self.session = env.get('HERDR_SESSION', 'assistant')
        self.default_profile = env.get('LEAD_DEFAULT_PROFILE', 'claude')
        self.models = {p: env.get('LEAD_MODEL_' + p.upper(), '').strip() for p in PROFILES}
        self.poll = float(env.get('POLL_SECONDS', '5'))
        self.idle_seconds = float(env.get('TOPIC_IDLE_DAYS', '7')) * 86400
        templates = Path(env.get('TEMPLATE_DIR', '/opt/herdr-runtime/templates'))
        self.task_template, self.topic_template = templates / 'lead-task.md', templates / 'topic.md'


class HerdrError(Exception):
    pass


class Herdr:
    """Thin argv wrapper around the herdr CLI for one session. Every result is herdr's JSON `result` object."""

    def __init__(self, session, binary='herdr', timeout=60):
        self.session, self.binary, self.timeout = session, binary, timeout

    def call(self, *args, timeout=None):
        try:
            p = subprocess.run([self.binary, '--session', self.session, *args], capture_output=True, text=True,
                               timeout=timeout or self.timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise HerdrError('timeout') from None
        except OSError as e:
            raise HerdrError('exec_' + type(e).__name__) from None
        try:
            # Success JSON goes to stdout; an error JSON goes to stderr with rc=1.
            data = json.loads(p.stdout if p.stdout.strip() else p.stderr)
        except ValueError:
            raise HerdrError('bad_output_rc_%d' % p.returncode) from None
        if data.get('error'):
            raise HerdrError(str(data['error'].get('code') or 'error'))
        return data.get('result') or {}

    def snapshot(self):
        return self.call('api', 'snapshot')

    def workspace(self, label, cwd):
        for w in self.call('workspace', 'list').get('workspaces', []):
            if w.get('label') == label:
                return w['workspace_id']
        return self.call('workspace', 'create', '--label', label, '--cwd', str(cwd), '--no-focus')['workspace']['workspace_id']

    def create_tab(self, workspace, label, cwd):
        r = self.call('tab', 'create', '--workspace', workspace, '--label', label, '--cwd', str(cwd), '--no-focus')
        return r['tab']['tab_id'], r['root_pane']['pane_id']

    def start_agent(self, name, kind, pane, args):
        return self.call('agent', 'start', name, '--kind', kind, '--pane', pane, '--timeout', '120000', '--', *args,
                         timeout=150)

    def agent(self, name):
        try:
            return self.call('agent', 'get', name).get('agent')
        except HerdrError as e:
            if str(e) == 'agent_not_found':
                return None
            raise

    def prompt(self, name, text):
        return self.call('agent', 'prompt', name, text)

    def close_pane(self, pane):
        return self.call('pane', 'close', pane)


SCHEMA = """
CREATE TABLE IF NOT EXISTS topics(
  id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL, state TEXT NOT NULL, dir TEXT NOT NULL,
  profile TEXT NOT NULL, leads INTEGER NOT NULL DEFAULT 0, agent_name TEXT, tab_id TEXT, pane_id TEXT,
  terminal_id TEXT, missing INTEGER NOT NULL DEFAULT 0, tui_polls INTEGER NOT NULL DEFAULT 0, closing_at REAL,
  created_at REAL NOT NULL, last_active_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS chat_topics(
  owner TEXT NOT NULL, chat_id TEXT NOT NULL, topic_id TEXT NOT NULL, used_at REAL NOT NULL,
  PRIMARY KEY(owner, chat_id, topic_id));
CREATE TABLE IF NOT EXISTS tasks(
  id TEXT PRIMARY KEY, owner TEXT NOT NULL, topic_id TEXT NOT NULL, request_key TEXT NOT NULL,
  generation INTEGER NOT NULL, text TEXT NOT NULL, state TEXT NOT NULL, boot_id TEXT NOT NULL, task_dir TEXT NOT NULL,
  parent_chat_id TEXT, prompted INTEGER NOT NULL DEFAULT 0, detail TEXT, block_hash TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events(
  task_id TEXT NOT NULL, seq INTEGER NOT NULL, event_id TEXT NOT NULL, kind TEXT NOT NULL, summary TEXT NOT NULL,
  question TEXT, occurred_at REAL NOT NULL, PRIMARY KEY(task_id, seq));
CREATE TABLE IF NOT EXISTS questions(
  task_id TEXT NOT NULL, qid TEXT NOT NULL, version INTEGER NOT NULL, text TEXT NOT NULL, source TEXT NOT NULL,
  state TEXT NOT NULL, answer TEXT, created_at REAL NOT NULL, answered_at REAL, PRIMARY KEY(task_id, qid));
CREATE TABLE IF NOT EXISTS receipts(
  request_key TEXT PRIMARY KEY, task_id TEXT NOT NULL, qid TEXT NOT NULL, version INTEGER NOT NULL, at REAL NOT NULL);
"""


def read_file(path, limit=65536):
    """Read an agent-written file without following a symlink (the agent controls these directories)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(fd, 'rb') as f:
        return f.read(limit).decode('utf-8', 'replace')


def clean_rel_path(path):
    """'a/./b.md' -> ['a', 'b.md']; None for an absolute path, any '..', NUL / backslash, or an over-long path."""
    if not isinstance(path, str) or len(path) > 512 or '\x00' in path or '\\' in path or path.startswith('/'):
        return None
    parts = [p for p in path.split('/') if p not in ('', '.')]
    return parts if parts and '..' not in parts else None


def entry_error(dfd, name):
    """Why `name` in directory `dfd` could not be opened: missing | symlink | not_directory | unreadable."""
    try:
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return 'missing'
    except OSError:
        return 'unreadable'
    if stat.S_ISLNK(st.st_mode):
        return 'symlink'
    return 'unreadable' if stat.S_ISDIR(st.st_mode) else 'not_directory'


def walk_dir(start, parts):
    """Open start/parts[0]/parts[1]/... as directories without following any symlink in `parts`. `start` is a
    trusted path or an fd (not closed). Returns (fd, None) or (None, entry_error(...))."""
    try:
        fd = os.open(start, os.O_RDONLY | os.O_DIRECTORY) if isinstance(start, str) else os.dup(start)
    except OSError:
        return None, 'missing'
    for p in parts:
        try:
            nfd = os.open(p, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        except OSError:
            err = entry_error(fd, p)
            os.close(fd)
            return None, err
        os.close(fd)
        fd = nfd
    return fd, None


def read_entry(dfd, name, rel):
    """(status, body) for regular file `name` in directory `dfd`: at most FILE_LIMIT bytes, never via a symlink."""
    try:
        # O_NONBLOCK: a FIFO planted in the task directory must not hang the request.
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
    except OSError:
        err = entry_error(dfd, name)
        return (404, {'error': 'not_found'}) if err == 'missing' else (400, {'error': err})
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        return 400, {'error': 'is_directory' if stat.S_ISDIR(st.st_mode) else 'not_regular_file'}
    with os.fdopen(fd, 'rb') as f:
        data = f.read(FILE_LIMIT + 1)
    truncated = len(data) > FILE_LIMIT
    data = data[:FILE_LIMIT]
    binary = b'\x00' in data
    # final=False drops a multi-byte character cut in half by the limit instead of turning it into U+FFFD.
    text = None if binary else codecs.getincrementaldecoder('utf-8')('replace').decode(data, final=not truncated)
    return 200, {'path': rel, 'size': st.st_size, 'limit': FILE_LIMIT, 'truncated': truncated, 'binary': binary,
                 'content': text}


def list_tree(dfd, prefix, depth, out):
    """Append the entries under directory `dfd` to `out` (symlinks listed, never followed). True if cut short."""
    with os.scandir(dfd) as it:
        entries = sorted(it, key=lambda e: e.name)
    for e in entries:
        if len(out) >= LIST_LIMIT:
            return True
        rel = prefix + e.name
        if e.is_symlink():
            out.append({'path': rel, 'type': 'symlink'})
        elif e.is_dir(follow_symlinks=False):
            out.append({'path': rel + '/', 'type': 'dir'})
            if depth + 1 < LIST_DEPTH:
                sub, err = walk_dir(dfd, [e.name])
                if sub is not None:
                    try:
                        if list_tree(sub, rel + '/', depth + 1, out):
                            return True
                    finally:
                        os.close(sub)
        else:
            st = e.stat(follow_symlinks=False)
            regular = stat.S_ISREG(st.st_mode)
            out.append({'path': rel, 'type': 'file' if regular else 'other', **({'size': st.st_size} if regular else {})})
    return False


def clean_name(text, limit=40):
    name = re.sub(r'\s+', ' ', re.sub(r'[\x00-\x1f\x7f]', ' ', text)).strip()
    return name[:limit] or 'topic'


class Bridge:
    def __init__(self, cfg, herdr, clock=time.time, sleep=time.sleep, spawn=None):
        self.cfg, self.herdr, self.clock, self.sleep = cfg, herdr, clock, sleep
        self.spawn = spawn or (lambda fn, *args: threading.Thread(target=fn, args=args, daemon=True).start())
        self.boot_id = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.topic_locks = {}
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(cfg.state_dir / 'ledger.sqlite3'), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        # The ledger lives on NFS: WAL needs shared memory between processes and is unsafe there. Rollback journal
        # (DELETE) + full sync, and this process is the only writer (one bridge per Pod, one Pod per volume).
        self.db.execute('PRAGMA journal_mode=DELETE')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript(SCHEMA)
        self.ready_at = 0.0

    # ---- ledger helpers (call with self.lock held) ----
    def tx(self):
        return _Tx(self)

    def task(self, tid):
        return self.db.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()

    def topic(self, topic_id):
        return self.db.execute('SELECT * FROM topics WHERE id=?', (topic_id,)).fetchone()

    def set(self, tid, **values):
        values['updated_at'] = self.clock()
        self._update('tasks', tid, values)

    def set_topic(self, topic_id, **values):
        self._update('topics', topic_id, values)

    def _update(self, table, key, values):
        cols = ', '.join(k + '=?' for k in values)
        self.db.execute('UPDATE ' + table + ' SET ' + cols + ' WHERE id=?', (*values.values(), key))

    def topic_lock(self, topic_id):
        with self.lock:
            return self.topic_locks.setdefault(topic_id, threading.Lock())

    def open_question(self, tid):
        return self.db.execute("SELECT * FROM questions WHERE task_id=? AND state='open' ORDER BY version DESC LIMIT 1",
                               (tid,)).fetchone()

    def emit(self, tid, kind, summary, question=None):
        t = self.task(tid)
        seq = self.db.execute('SELECT COUNT(*) FROM events WHERE task_id=?', (tid,)).fetchone()[0] + 1
        if seq > EVENT_LIMIT:
            self.set(tid, detail='event_limit')
            log('event_limit', tid)
            return
        topic = self.topic(t['topic_id'])
        self.db.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?)',
                        (tid, seq, tid + ':' + str(seq), kind, ('[topic %s] ' % topic['name'] + summary)[:8000],
                         json.dumps(question, ensure_ascii=False) if question else None, self.clock()))
        log('event', tid, seq, kind, 'generation', t['generation'])

    def finish(self, tid, kind, summary):
        """Terminal transition: revoke open questions, set the state and emit the matching event."""
        self.db.execute("UPDATE questions SET state='revoked' WHERE task_id=? AND state='open'", (tid,))
        self.set(tid, state=kind)
        self.emit(tid, kind, summary)

    def ask(self, tid, text, source):
        """Register a question for the human. Returns (qid, version) or None when the task cannot take one."""
        t = self.task(tid)
        if not t or t['state'] not in ACTIVE:
            return None
        n = self.db.execute('SELECT COUNT(*) FROM questions WHERE task_id=?', (tid,)).fetchone()[0] + 1
        if n > MAX_QUESTIONS:
            self.set(tid, detail='question_limit')
            return None
        self.db.execute("UPDATE questions SET state='revoked' WHERE task_id=? AND state='open'", (tid,))
        qid, text = 'q' + str(n), text.strip()[:2000] or '(empty question)'
        self.db.execute("INSERT INTO questions VALUES (?,?,?,?,?,'open',NULL,?,NULL)",
                        (tid, qid, n, text, source, self.clock()))
        self.set(tid, state='blocked')
        self.emit(tid, 'needs_input', '等待回答：' + text[:200], {'id': qid, 'version': n, 'text': text})
        return qid, n

    def active_tasks(self, topic_id):
        return self.db.execute('SELECT * FROM tasks WHERE topic_id=? AND state IN (?,?,?) ORDER BY created_at',
                               (topic_id, *ACTIVE)).fetchall()

    # ---- topic resolution ----
    def resolve_topic(self, owner, topic, chat, text, profile):
        """Named topic (id or name) > the chat's last topic > a new topic. Called inside a transaction."""
        now = self.clock()
        row = None
        if topic:
            row = self.db.execute('SELECT * FROM topics WHERE owner=? AND (id=? OR lower(name)=lower(?)) '
                                  'ORDER BY last_active_at DESC LIMIT 1', (owner, topic, clean_name(topic))).fetchone()
        elif chat:
            row = self.db.execute('SELECT t.* FROM chat_topics c JOIN topics t ON t.id=c.topic_id '
                                  'WHERE c.owner=? AND c.chat_id=? ORDER BY c.used_at DESC LIMIT 1',
                                  (owner, chat)).fetchone()
        if row is None:
            topic_id = 'topic_' + uuid.uuid4().hex[:12]
            name = clean_name(topic if topic and not TOPIC_ID.fullmatch(topic) else text.splitlines()[0])
            self.db.execute('INSERT INTO topics (id, owner, name, state, dir, profile, created_at, last_active_at) '
                            'VALUES (?,?,?,?,?,?,?,?)',
                            (topic_id, owner, name, 'open', str(self.cfg.ws_root / 'topics' / topic_id), profile,
                             now, now))
            row = self.topic(topic_id)
            log('topic', topic_id, 'new')
        elif row['state'] == 'closing':
            self.set_topic(row['id'], state='open', closing_at=None)   # new work cancels the wind-down
        if chat:
            self.db.execute('INSERT INTO chat_topics VALUES (?,?,?,?) ON CONFLICT(owner, chat_id, topic_id) '
                            'DO UPDATE SET used_at=excluded.used_at', (owner, chat, row['id'], now))
        self.set_topic(row['id'], last_active_at=now)
        return row

    # ---- REST operations: return (http_status, body) ----
    def list_tasks(self, owner):
        with self.lock:
            rows = self.db.execute('SELECT id, topic_id, state, created_at, updated_at FROM tasks WHERE owner=? '
                                   'ORDER BY created_at DESC LIMIT 100', (owner,)).fetchall()
        return 200, {'tasks': [{'id': r['id'], 'topic_id': r['topic_id'], 'state': self.public_state(r['state']),
                                'created_at': r['created_at'], 'updated_at': r['updated_at']} for r in rows]}

    def list_topics(self, owner):
        with self.lock:
            rows = self.db.execute('SELECT * FROM topics WHERE owner=? ORDER BY last_active_at DESC', (owner,)).fetchall()
            out = [{'id': r['id'], 'name': r['name'], 'state': r['state'], 'profile': r['profile'],
                    'lead': r['agent_name'], 'active_tasks': len(self.active_tasks(r['id'])),
                    'last_active_at': r['last_active_at']} for r in rows]
        return 200, {'topics': out}

    def create(self, owner, body):
        tid, text = body.get('id'), body.get('task')
        if not isinstance(tid, str) or not TASK_ID.fullmatch(tid):
            return 400, {'error': 'task_id'}
        if not isinstance(text, str) or not text.strip() or len(text) > 8000:
            return 400, {'error': 'task_length'}
        generation, key = body.get('generation', 1), body.get('request_key') or ''
        topic, chat = body.get('topic'), body.get('parent_chat_id')
        if (type(generation) is not int or generation < 1 or not isinstance(key, str) or len(key) > 128
                or (topic is not None and (not isinstance(topic, str) or not topic.strip() or len(topic) > 200))
                or (chat is not None and (not isinstance(chat, str) or not chat or len(chat) > 128))):
            return 400, {'error': 'bad_body'}
        lines = text.strip().splitlines()
        m = PROFILE_LINE.fullmatch(lines[0].strip())
        profile = m[1] if m else self.cfg.default_profile
        if m:
            text = '\n'.join(lines[1:]).strip() or text
        with self.lock, self.tx():
            t = self.task(tid)
            if t:
                if t['owner'] != owner:
                    return 404, {'error': 'not_found'}
                if t['request_key'] != key or t['generation'] != generation:
                    return 409, {'error': 'conflict'}
                tp = self.topic(t['topic_id'])
                return 200, {'id': tid, 'status': 'existing', 'topic': {'id': tp['id'], 'name': tp['name']}}
            tp = self.resolve_topic(owner, topic.strip() if topic else None, chat, text, profile)
            now = self.clock()
            self.db.execute('INSERT INTO tasks (id, owner, topic_id, request_key, generation, text, state, boot_id, '
                            'task_dir, parent_chat_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                            (tid, owner, tp['id'], key, generation, text, 'accepted', self.boot_id,
                             str(Path(tp['dir']) / 'tasks' / tid), chat, now, now))
        log('task', tid, 'topic', tp['id'])
        self.spawn(self.launch, tid)
        return 200, {'id': tid, 'status': 'started', 'topic': {'id': tp['id'], 'name': tp['name']}}

    @staticmethod
    def public_state(state):
        return 'running' if state == 'accepted' else state

    def events(self, owner, tid):
        with self.lock:
            t = self.task(tid)
            if not t or t['owner'] != owner:
                return 404, {'error': 'not_found'}
            rows = self.db.execute('SELECT * FROM events WHERE task_id=? ORDER BY seq', (tid,)).fetchall()
            q = self.open_question(tid)
            tp = self.topic(t['topic_id'])
        return 200, {
            'state': self.public_state(t['state']),
            'question': {'id': q['qid'], 'version': q['version'], 'text': q['text']} if q else None,
            'topic': {'id': tp['id'], 'name': tp['name']},
            'events': [{'event_id': r['event_id'], 'seq': r['seq'], 'task_id': tid, 'generation': t['generation'],
                        'kind': r['kind'], 'summary': r['summary'],
                        'question': json.loads(r['question']) if r['question'] else None,
                        'occurred_at': r['occurred_at']} for r in rows]}

    def reply(self, owner, tid, body):
        qid, version, answer, key = (body.get('question_id'), body.get('version'), body.get('answer'),
                                     body.get('request_key'))
        if (not isinstance(qid, str) or type(version) is not int or not isinstance(answer, str)
                or not answer.strip() or len(answer) > 4000 or not isinstance(key, str) or not key or len(key) > 256):
            return 400, {'error': 'bad_body'}
        with self.lock, self.tx():
            t = self.task(tid)
            if not t or t['owner'] != owner:
                return 404, {'error': 'not_found'}
            # Receipt before the current-question check: a retry after a lost response is safe.
            r = self.db.execute('SELECT * FROM receipts WHERE request_key=?', (key,)).fetchone()
            if r:
                if r['task_id'] != tid:
                    return 409, {'error': 'conflict'}
                return 200, {'status': 'already_answered', 'question_id': r['qid'], 'version': r['version']}
            q = self.open_question(tid)
            if not q or q['qid'] != qid or q['version'] != version or t['state'] != 'blocked':
                return 409, {'error': 'stale_question'}
            now = self.clock()
            self.db.execute("UPDATE questions SET state='answered', answer=?, answered_at=? WHERE task_id=? AND qid=?",
                            (answer, now, tid, qid))
            self.db.execute('INSERT INTO receipts VALUES (?,?,?,?,?)', (key, tid, qid, version, now))
            self.set(tid, state='running')
            self.set_topic(t['topic_id'], tui_polls=0, last_active_at=now)
            source, qtext = q['source'], q['text']
        log('reply', tid, qid, version)
        self.spawn(self.deliver, tid, qid, version, qtext, answer, source)
        return 200, {'status': 'answered', 'question_id': qid, 'version': version}

    # ---- read-only access to a task's own directory ----
    def open_task_dir(self, owner, tid):
        """(fd of the caller's task directory, None) or (None, (status, body)). Every component below WS_ROOT is
        opened without following symlinks, so a swapped-in link anywhere on the way cannot point elsewhere."""
        with self.lock:
            t = self.task(tid) if isinstance(tid, str) and TASK_ID.fullmatch(tid) else None
        if not t or t['owner'] != owner:
            return None, (404, {'error': 'not_found'})
        try:
            rel = Path(t['task_dir']).relative_to(self.cfg.ws_root).parts
        except ValueError:
            return None, (404, {'error': 'not_found'})
        fd, err = walk_dir(str(self.cfg.ws_root), rel)
        if err:
            return None, ((404, {'error': 'no_task_dir'}) if err == 'missing' else (400, {'error': err}))
        return fd, None

    def list_files(self, owner, tid):
        fd, err = self.open_task_dir(owner, tid)
        if err:
            return err
        try:
            files = []
            truncated = list_tree(fd, '', 0, files)
        finally:
            os.close(fd)
        return 200, {'task_id': tid, 'files': files, 'truncated': truncated}

    def read_task_file(self, owner, tid, body):
        path = body.get('path') or 'REPORT.md'
        parts = clean_rel_path(path)
        if parts is None:
            return 400, {'error': 'bad_path'}
        fd, err = self.open_task_dir(owner, tid)
        if err:
            return err
        try:
            dfd, err = walk_dir(fd, parts[:-1])
            if err:
                return (404 if err == 'missing' else 400), {'error': 'not_found' if err == 'missing' else err}
            try:
                return read_entry(dfd, parts[-1], '/'.join(parts))
            finally:
                os.close(dfd)
        finally:
            os.close(fd)

    def close_topic(self, owner, topic_id):
        with self.lock, self.tx():
            tp = self.topic(topic_id)
            if not tp or tp['owner'] != owner:
                return 404, {'error': 'not_found'}
            if self.active_tasks(topic_id):
                return 409, {'error': 'active_tasks'}
            if tp['state'] == 'open':
                self.begin_close(tp)
            tp = self.topic(topic_id)
        return 200, {'id': topic_id, 'state': tp['state']}

    # ---- internal endpoint for task-ask ----
    def internal_ask(self, body):
        tid, text = body.get('task_id'), body.get('text')
        if not isinstance(tid, str) or not TASK_ID.fullmatch(tid) or not isinstance(text, str) or not text.strip():
            return 400, {'error': 'bad_body'}
        with self.lock, self.tx():
            r = self.ask(tid, text, 'ask')
        if not r:
            return 409, {'error': 'task_not_active'}
        return 200, {'question_id': r[0], 'version': r[1]}

    def internal_question(self, tid, qid):
        with self.lock:
            q = self.db.execute('SELECT * FROM questions WHERE task_id=? AND qid=?', (tid, qid)).fetchone()
        if not q:
            return 404, {'error': 'not_found'}
        return 200, {'question_id': qid, 'version': q['version'], 'state': q['state'], 'answer': q['answer']}

    # ---- execution ----
    def render(self, template, fallback, values, text=None):
        body = template.read_text() if template.is_file() else fallback
        for k, v in values.items():
            body = body.replace('{{' + k + '}}', v)
        return body if text is None else body.replace('{{TASK_TEXT}}', text)  # last: never expand the user's text

    def values(self, tp, task_dir=None):
        v = {'TOPIC_ID': tp['id'], 'TOPIC_NAME': tp['name'], 'TOPIC_DIR': tp['dir'], 'WS_ROOT': str(self.cfg.ws_root),
             'NOTES': str(Path(tp['dir']) / 'NOTES.md'), 'PROFILE': tp['profile']}
        if task_dir:
            v.update(TASK_ID=task_dir.name, TASK_DIR=str(task_dir), REPORT=str(task_dir / 'REPORT.md'),
                     ANSWERS=str(task_dir / 'answers'))
        return v

    def ensure_lead(self, tp):
        """Return (agent_name, started_now) for the topic's live lead, starting a new one if needed."""
        if tp['agent_name'] and tp['terminal_id']:
            a = self.herdr.agent(tp['agent_name'])
            if a and a.get('terminal_id') == tp['terminal_id']:
                return tp['agent_name'], False
        profile, model = tp['profile'], self.cfg.models.get(tp['profile'], '')
        if profile not in PROFILES or not model:
            raise HerdrError('profile %r is not allowed or LEAD_MODEL_%s is not set (CLI defaults are never used)'
                             % (profile, profile.upper()))
        topic_dir = Path(tp['dir'])
        topic_dir.mkdir(parents=True, exist_ok=True)
        (topic_dir / 'TOPIC.md').write_text(self.render(self.cfg.topic_template, DEFAULT_TOPIC, self.values(tp)))
        n = tp['leads'] + 1
        name = 'lead-%s-%d' % (tp['id'][6:], n)   # unique for the life of the herdr session
        ws = self.herdr.workspace('assistant', self.cfg.ws_root)
        tab, pane = self.herdr.create_tab(ws, 'T·' + tp['name'][:24], topic_dir)
        with self.lock, self.tx():
            self.set_topic(tp['id'], leads=n, agent_name=name, tab_id=tab, pane_id=pane, terminal_id=None)
        self.herdr.start_agent(name, profile, pane, [*PROFILES[profile], '--model', model])
        a = self.herdr.agent(name)
        if not a:
            raise HerdrError('agent_missing_after_start')
        with self.lock, self.tx():
            self.set_topic(tp['id'], terminal_id=a.get('terminal_id'), state='open', missing=0, tui_polls=0,
                           closing_at=None)
        log('lead', tp['id'], name, pane)
        return name, True

    def launch(self, tid):
        with self.lock:
            t = self.task(tid)
        task_dir = Path(t['task_dir'])
        step = 'task_dir'
        try:
            with self.topic_lock(t['topic_id']):
                with self.lock:
                    tp = self.topic(t['topic_id'])
                (task_dir / 'answers').mkdir(parents=True, exist_ok=True)
                brief = task_dir / 'TASK.md'
                if not brief.exists():
                    brief.write_text(self.render(self.cfg.task_template, DEFAULT_TASK, self.values(tp, task_dir),
                                                 t['text']))
                step = 'lead'
                name, fresh = self.ensure_lead(tp)
                with self.lock, self.tx():
                    self.set(tid, state='running')
                before = self.herdr.agent(name) or {}
                if before.get('agent_status') == 'blocked':
                    # herdr rejects prompts to a blocked agent; the task is sent once the human has unblocked it.
                    with self.lock, self.tx():
                        self.ask(tid, self.takeover_text(tp, name, '停在 CLI 自己的交互界面，新任务书还没送达'), 'tui')
                    return
                step = 'prompt'
                self.send_brief(tid, name, fresh)
                if fresh and not self.confirm_working(name, before, task_dir):
                    # herdr reports a CLI as ready while it still shows first-run screens (theme, login, trust),
                    # and the prompt is swallowed there. Never type into it again blindly: ask for a takeover.
                    with self.lock, self.tx():
                        self.set(tid, prompted=0)
                        self.ask(tid, self.takeover_text(tp, name, '启动后 %d 秒内没有开始工作，可能停在首次引导 / 登录 / 信任界面，'
                                                         '任务 prompt 可能被吞' % PROMPT_CONFIRM_SECONDS), 'tui')
        except (HerdrError, OSError, KeyError) as e:
            log('launch_failed', tid, step, type(e).__name__, e)
            with self.lock, self.tx():
                self.finish(tid, 'failed', '派给 lead 失败（步骤 %s：%s）。未自动重试；pane 如已创建会保留供人工核对。'
                            % (step, str(e)[:200]))

    def send_brief(self, tid, name, fresh):
        with self.lock:
            t = self.task(tid)
            tp = self.topic(t['topic_id'])
        brief = Path(t['task_dir']) / 'TASK.md'
        if fresh:
            text = '读 %s/TOPIC.md，然后读 %s 并照做。' % (tp['dir'], brief)
        else:
            # Prompts to a working agent are queued by herdr until its current turn ends.
            text = '新任务（与其它在飞任务并行，各写各的 REPORT.md）：读 %s 并照做。' % brief
        self.herdr.prompt(name, text)
        with self.lock, self.tx():
            self.set(tid, prompted=1)
        log('prompted', tid, name)

    @staticmethod
    def takeover_text(tp, name, what):
        return ('topic「%s」的 lead %s %s。bridge 不会代按按键。请 Owner 用 herdr machine 接管 tab T·%s 处理，'
                '处理完回答任意内容，bridge 会（重新）送达任务书并继续跟踪。' % (tp['name'], name, what, tp['name'][:24]))

    def confirm_working(self, name, before, task_dir):
        """True once the lead is working/blocked, has changed state since the prompt, or has started the report."""
        deadline = self.clock() + PROMPT_CONFIRM_SECONDS
        while True:
            a = self.herdr.agent(name)
            if a and (a.get('agent_status') in ('working', 'blocked')
                      or a.get('state_change_seq') != before.get('state_change_seq')):
                return True
            if read_file(os.path.join(str(task_dir), 'REPORT.md')) is not None:
                return True
            if self.clock() >= deadline:
                return False
            self.sleep(2)

    def deliver(self, tid, qid, version, question, answer, source):
        """Hand an answer to the lead: a file in answers/ plus a prompt naming it. After a takeover the task brief
        itself may still be undelivered; send it now."""
        with self.lock:
            t = self.task(tid)
            tp = self.topic(t['topic_id'])
        path = Path(t['task_dir']) / 'answers' / ('%s.md' % qid)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# %s（version %d）\n\n## 问题\n\n%s\n\n## 用户回答\n\n%s\n' % (qid, version, question, answer))
            if not tp['agent_name']:
                return
            if not t['prompted']:
                self.send_brief(tid, tp['agent_name'], True)
            elif source != 'tui':
                self.herdr.prompt(tp['agent_name'], '任务 %s 的问题 %s 有回答了：见 %s。读完按回答继续该任务。'
                                  % (tid, qid, path))
        except (HerdrError, OSError) as e:
            log('deliver_failed', tid, qid, type(e).__name__, e)
            with self.lock, self.tx():
                self.set(tid, detail='deliver_failed:%s:%s' % (qid, str(e)[:80]))

    def observe_task(self, t):
        """REPORT.md first line of one prompted task -> events. Never inferred from the lead's herdr state."""
        tid = t['id']
        report = read_file(os.path.join(t['task_dir'], 'REPORT.md'))
        lines = (report or '').splitlines()
        m = STATUS_LINE.match(lines[0]) if lines else None
        status, rest = (m[1], m[2].strip()) if m else (None, '')
        with self.lock, self.tx():
            t = self.task(tid)
            if t['state'] not in ('running', 'blocked'):
                return
            if status == 'done':
                self.finish(tid, 'completed', '报告写了 STATUS: done（待验收，不等于已批准）。报告：%s/REPORT.md\n%s'
                            % (t['task_dir'], '\n'.join(lines[1:]).strip()[:SUMMARY_CHARS]))
                self.set_topic(t['topic_id'], last_active_at=self.clock())
                return
            if status == 'blocked' and not self.open_question(tid):
                h = hashlib.sha256('\n'.join(lines[:40]).encode()).hexdigest()
                if h != t['block_hash']:
                    self.set(tid, block_hash=h)
                    self.ask(tid, '%s\n\n（来自 %s/REPORT.md）\n%s' % (rest, t['task_dir'], '\n'.join(lines[1:30])),
                             'report')

    def observe_topic(self, tp):
        """The topic's lead as a whole: vanished, stuck in a CLI dialog, idle long enough to close."""
        try:
            agent = self.herdr.agent(tp['agent_name'])
        except HerdrError as e:
            log('observe_topic', tp['id'], e)
            return
        with self.lock, self.tx():
            tp = self.topic(tp['id'])
            if not tp['agent_name']:
                return
            active = self.active_tasks(tp['id'])
            if agent is None or agent.get('terminal_id') != tp['terminal_id']:
                missing = tp['missing'] + 1
                self.set_topic(tp['id'], missing=missing)
                if missing >= MISSING_POLLS or tp['state'] == 'closing':
                    self.lead_gone(tp, active, 'lead %s 已不在 herdr 中，任务报告未写 STATUS: done。' % tp['agent_name'])
                return
            self.set_topic(tp['id'], missing=0)
            now = self.clock()
            if tp['state'] == 'closing':
                notes = Path(tp['dir']) / 'NOTES.md'
                written = notes.is_file() and not notes.is_symlink() and notes.stat().st_mtime >= tp['closing_at']
                if written or now - tp['closing_at'] > CLOSE_WAIT_SECONDS:
                    self.spawn(self.close_lead, tp['id'])
                return
            if agent.get('agent_status') == 'blocked' and active:
                polls = tp['tui_polls'] + 1
                self.set_topic(tp['id'], tui_polls=polls)
                target = [t for t in active if not self.open_question(t['id'])]
                if polls == TUI_POLLS and target:
                    self.ask(target[-1]['id'], self.takeover_text(tp, tp['agent_name'], '停在 CLI 自己的交互界面'
                                                                  '（权限 / 登录 / 确认框）'), 'tui')
                return
            self.set_topic(tp['id'], tui_polls=0)
            if not active and tp['state'] == 'open' and now - tp['last_active_at'] > self.cfg.idle_seconds:
                self.begin_close(tp)

    def lead_gone(self, tp, active, why):
        for t in active:
            if t['prompted']:
                self.finish(t['id'], 'failed', why + '任务目录：%s' % t['task_dir'])
        state = 'closed' if tp['state'] == 'closing' else 'open'
        self.set_topic(tp['id'], agent_name=None, terminal_id=None, pane_id=None, tab_id=None, missing=0,
                       state=state, closing_at=None)

    def begin_close(self, tp):
        """Ask the lead to write NOTES.md; observe_topic closes the pane once it is written (or after a timeout)."""
        if not tp['agent_name']:
            self.set_topic(tp['id'], state='closed')
            return
        self.set_topic(tp['id'], state='closing', closing_at=self.clock())
        self.spawn(self.prompt_quietly, tp['agent_name'],
                   'topic「%s」要收尾了：把这个 topic 的结论、现状、未完成事项、下次接手要知道的写进 %s/NOTES.md'
                   '（已有就更新），写完就停，不要再开新工作。' % (tp['name'], tp['dir']))
        log('topic_closing', tp['id'])

    def prompt_quietly(self, name, text):
        try:
            self.herdr.prompt(name, text)
        except HerdrError as e:
            log('prompt_failed', name, e)

    def close_lead(self, topic_id):
        with self.lock:
            tp = self.topic(topic_id)
        if not tp or tp['state'] != 'closing':
            return
        try:
            if tp['pane_id']:
                self.herdr.close_pane(tp['pane_id'])
        except HerdrError as e:
            log('close_pane_failed', topic_id, e)
        with self.lock, self.tx():
            tp = self.topic(topic_id)
            if tp['state'] == 'closing':
                self.set_topic(topic_id, state='closed', agent_name=None, terminal_id=None, pane_id=None, tab_id=None,
                               closing_at=None)
                log('topic_closed', topic_id)

    def observe_all(self):
        with self.lock:
            tasks = self.db.execute("SELECT * FROM tasks WHERE state IN ('running','blocked') AND prompted=1").fetchall()
            topics = self.db.execute("SELECT * FROM topics WHERE state IN ('open','closing') AND agent_name IS NOT NULL "
                                     "AND terminal_id IS NOT NULL").fetchall()
        for t in tasks:
            try:
                self.observe_task(t)
            except Exception as e:  # one bad task must not stop the loop
                log('observe_error', t['id'], type(e).__name__, e)
        for tp in topics:
            try:
                self.observe_topic(tp)
            except Exception as e:
                log('observe_error', tp['id'], type(e).__name__, e)

    def recover(self):
        """At startup: adopt leads whose herdr terminal is still alive; tasks of any other lead are interrupted."""
        with self.lock:
            topics = self.db.execute('SELECT * FROM topics WHERE agent_name IS NOT NULL').fetchall()
        alive = set()
        for tp in topics:
            try:
                a = self.herdr.agent(tp['agent_name'])
            except HerdrError:
                a = None
            with self.lock, self.tx():
                if a and tp['terminal_id'] and a.get('terminal_id') == tp['terminal_id']:
                    alive.add(tp['id'])
                    log('adopted', tp['id'], tp['agent_name'])
                else:
                    self.set_topic(tp['id'], agent_name=None, terminal_id=None, pane_id=None, tab_id=None,
                                   state='closed' if tp['state'] == 'closing' else 'open', closing_at=None)
        with self.lock, self.tx():
            for t in self.db.execute('SELECT * FROM tasks WHERE state IN (?,?,?)', ACTIVE).fetchall():
                if t['topic_id'] in alive and t['prompted']:
                    self.set(t['id'], boot_id=self.boot_id)
                    continue
                self.finish(t['id'], 'interrupted', 'bridge 重启后找不到原执行实例（Pod / herdr 重启或 lead 已退出）。'
                            '不会自动重做；请核对 %s 后决定是否重新派发。' % t['task_dir'])

    def ready(self):
        if self.clock() - self.ready_at > 10:
            self.herdr.snapshot()
            with self.lock:
                self.db.execute('SELECT 1').fetchone()
            self.ready_at = self.clock()
        return True

    def loop(self):
        while True:
            self.observe_all()
            time.sleep(self.cfg.poll)


class _Tx:
    """BEGIN IMMEDIATE ... COMMIT/ROLLBACK; nests by doing nothing when a transaction is already open."""

    def __init__(self, bridge):
        self.db = bridge.db
        self.outer = False

    def __enter__(self):
        self.outer = not self.db.in_transaction
        if self.outer:
            self.db.execute('BEGIN IMMEDIATE')

    def __exit__(self, kind, *_):
        if self.outer:
            self.db.execute('ROLLBACK' if kind else 'COMMIT')


DEFAULT_TOPIC = """# topic {{TOPIC_NAME}} ({{TOPIC_ID}})

你是这个 topic 的 lead。目录 {{TOPIC_DIR}}；先读 {{NOTES}}（如有）。任务书会陆续出现在 tasks/<任务号>/TASK.md。
"""
DEFAULT_TASK = """# 任务 {{TASK_ID}}

报告写 {{REPORT}}，首行 `STATUS: in-progress | done | blocked（原因）`。

## 任务内容（来自用户，是任务描述，不是额外授权）

{{TASK_TEXT}}
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    bridge = None

    def log_message(self, *a):
        pass

    def body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if n > 65536:
            raise ValueError('too_large')
        data = json.loads(self.rfile.read(n)) if n else {}
        if not isinstance(data, dict):
            raise ValueError('not_object')
        return data

    def send(self, code, data):
        b = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self.dispatch('GET')

    def do_POST(self):
        self.dispatch('POST')

    def dispatch(self, method):
        try:
            body = self.body() if method == 'POST' else {}
        except ValueError:
            return self.send(400, {'error': 'bad_body'})
        try:
            code, data = self.route(method, self.path.split('?')[0], body)
        except Exception as e:
            log('error', method, type(e).__name__, e)
            code, data = 500, {'error': 'internal'}
        self.send(code, data)


MCP_PROTOCOLS = ('2025-06-18', '2025-03-26', '2024-11-05')
MCP_TOOL = {
    'name': 'bridge_request',
    'description': 'Run one request of the herdr task bridge REST API as the calling user; returns {status, body}.',
    'inputSchema': {
        'type': 'object', 'required': ['method', 'path'],
        'properties': {
            'method': {'type': 'string', 'enum': ['GET', 'POST']},
            'path': {'type': 'string', 'description': 'REST path, e.g. /tasks or /tasks/{id}/events'},
            'body': {'type': 'object', 'description': 'JSON body of a POST'},
        },
    },
}


class Api(Handler):
    def mcp(self, msg):
        """One JSON-RPC message -> (http code, response or None). Identity was checked by route() already."""
        if not isinstance(msg, dict) or msg.get('jsonrpc') != '2.0' or not isinstance(msg.get('method'), str):
            return 400, {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'invalid request'}}
        mid, method, params = msg.get('id'), msg['method'], msg.get('params') or {}
        if mid is None:  # notification
            return 202, None
        def ok(result):
            return 200, {'jsonrpc': '2.0', 'id': mid, 'result': result}
        def err(code, text):
            return 200, {'jsonrpc': '2.0', 'id': mid, 'error': {'code': code, 'message': text}}
        if method == 'initialize':
            want = params.get('protocolVersion')
            return ok({'protocolVersion': want if want in MCP_PROTOCOLS else MCP_PROTOCOLS[0],
                       'capabilities': {'tools': {'listChanged': False}},
                       'serverInfo': {'name': 'herdr-bridge', 'version': '1'}})
        if method == 'ping':
            return ok({})
        if method == 'tools/list':
            return ok({'tools': [MCP_TOOL]})
        if method != 'tools/call':
            return err(-32601, 'method not found')
        if params.get('name') != MCP_TOOL['name']:
            return err(-32602, 'unknown tool')
        a = params.get('arguments') or {}
        rest_method, path, body = a.get('method'), a.get('path'), a.get('body') or {}
        if rest_method not in ('GET', 'POST') or not isinstance(path, str) or not path.startswith('/') \
                or not isinstance(body, dict) or path.split('?')[0] in ('/mcp', '/healthz', '/readyz'):
            return err(-32602, 'invalid arguments')
        code, data = self.route(rest_method, path.split('?')[0], body if rest_method == 'POST' else {})
        result = {'status': code, 'body': data}
        return ok({'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}],
                   'structuredContent': result, 'isError': False})

    def dispatch(self, method):
        if self.path.split('?')[0] != '/mcp':
            return super().dispatch(method)
        if method != 'POST':
            return self.send(405, {'error': 'method'})
        sub = self.headers.get(SUB_HEADER, '')
        if sub not in self.bridge.cfg.allowed:
            log('deny', 'MCP', 'sub_present=' + str(bool(sub)))
            return self.send(403, {'error': 'identity'})
        try:
            msg = self.body()
        except ValueError:
            return self.send(400, {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'parse error'}})
        try:
            code, data = self.mcp(msg)
        except Exception as e:
            log('error', 'MCP', type(e).__name__, e)
            code, data = 200, {'jsonrpc': '2.0', 'id': msg.get('id'), 'error': {'code': -32603, 'message': 'internal'}}
        if data is None:
            self.send_response(code)
            self.send_header('Content-Length', '0')
            return self.end_headers()
        self.send(code, data)

    def route(self, method, path, body):
        b = self.bridge
        if path == '/healthz' and method == 'GET':
            return 200, {'ok': True}
        if path == '/readyz' and method == 'GET':
            try:
                b.ready()
            except Exception as e:
                return 503, {'ok': False, 'error': type(e).__name__}
            return 200, {'ok': True}
        sub = self.headers.get(SUB_HEADER, '')
        if sub not in b.cfg.allowed:
            log('deny', method, 'sub_present=' + str(bool(sub)))
            return 403, {'error': 'identity'}
        if path == '/tasks':
            return b.list_tasks(sub) if method == 'GET' else b.create(sub, body)
        if path == '/topics' and method == 'GET':
            return b.list_topics(sub)
        parts = path.strip('/').split('/')
        if len(parts) == 3 and parts[0] == 'tasks':
            if parts[2] == 'events' and method == 'GET':
                return b.events(sub, parts[1])
            if parts[2] == 'reply' and method == 'POST':
                return b.reply(sub, parts[1], body)
            if parts[2] == 'files' and method == 'GET':
                return b.list_files(sub, parts[1])
            if parts[2] == 'read' and method == 'POST':
                return b.read_task_file(sub, parts[1], body)
        if len(parts) == 3 and parts[0] == 'topics' and parts[2] == 'close' and method == 'POST':
            return b.close_topic(sub, parts[1])
        return 404, {'error': 'path'}


class Internal(Handler):
    def route(self, method, path, body):
        b = self.bridge
        if path == '/healthz':
            return 200, {'ok': True}
        if path == '/internal/ask' and method == 'POST':
            return b.internal_ask(body)
        parts = path.strip('/').split('/')
        if len(parts) == 4 and parts[:2] == ['internal', 'questions'] and method == 'GET':
            return b.internal_question(parts[2], parts[3])
        return 404, {'error': 'path'}


def serve(bridge):
    api = type('BoundApi', (Api,), {'bridge': bridge})
    internal = type('BoundInternal', (Internal,), {'bridge': bridge})
    threading.Thread(target=ThreadingHTTPServer(('127.0.0.1', bridge.cfg.internal_port), internal).serve_forever,
                     daemon=True).start()
    return ThreadingHTTPServer((bridge.cfg.host, bridge.cfg.port), api)


def main():
    cfg = Config()
    if not cfg.allowed:
        sys.exit('ALLOWED_SUBS is empty')
    bridge = Bridge(cfg, Herdr(cfg.session))
    bridge.recover()
    threading.Thread(target=bridge.loop, daemon=True).start()
    server = serve(bridge)
    usable = [p for p, m in cfg.models.items() if m]
    log('herdr bridge on :%d (internal 127.0.0.1:%d), allowed subs: %d, profiles with a model: %s'
        % (cfg.port, cfg.internal_port, len(cfg.allowed), ','.join(usable) or 'none'))
    server.serve_forever()


if __name__ == '__main__':
    main()
