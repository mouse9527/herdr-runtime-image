import importlib.machinery, importlib.util, json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OPT = ROOT / 'rootfs/opt/herdr-runtime'
_loader = importlib.machinery.SourceFileLoader('supervisor', str(OPT / 'bin/herdr-runtime-supervisor'))
_spec = importlib.util.spec_from_loader('supervisor', _loader)
sup = importlib.util.module_from_spec(_spec)
_loader.exec_module(sup)


def test_link_replaces_stale_links_but_keeps_user_files(tmp_path):
    target = tmp_path / 'skill'
    target.mkdir()
    fresh, stale, user = tmp_path / 'a/fresh', tmp_path / 'a/stale', tmp_path / 'a/user'
    stale.parent.mkdir()
    stale.symlink_to(tmp_path / 'old')
    user.write_text('mine')
    for p in (fresh, stale, user):
        sup.link(target, p)
    assert fresh.resolve() == target and stale.resolve() == target
    assert user.read_text() == 'mine' and not user.is_symlink()


def test_ws_template_is_copied_without_overwriting(tmp_path):
    ws = tmp_path / 'ws'
    (ws).mkdir()
    (ws / 'README.md').write_text('edited by the lead')
    sup.copy_missing(OPT / 'templates/ws', ws)
    assert (ws / 'README.md').read_text() == 'edited by the lead'
    assert (ws / '.templates/task.md').is_file()
    assert (ws / 'CLAUDE.md').is_symlink() and (ws / 'AGENTS.md').is_symlink()
    assert (ws / 'NOW.md').is_file()



def test_herdr_session_dir_is_local_and_layout_state_round_trips(tmp_path, monkeypatch):
    home, run = tmp_path / 'home', tmp_path / 'run'
    monkeypatch.setattr(sup, 'HOME', home)
    monkeypatch.setattr(sup, 'HERDR_LOCAL', run / 'herdr')
    monkeypatch.setattr(sup, 'HERDR_SAVED', home / '.local/state/herdr-runtime/herdr-session')
    # an older image kept the whole session (sockets included) on the volume
    old = home / '.config/herdr/sessions' / sup.SESSION
    (old / 'session-snapshots').mkdir(parents=True)
    (old / 'session.json').write_text('{"workspaces": ["assistant"]}')
    (old / 'session-snapshots/s1.json').write_text('{}')
    (old / 'herdr.sock').write_text('')
    local = sup.prepare_herdr_session()
    link = home / '.config/herdr/sessions'
    assert link.is_symlink() and (link / sup.SESSION).resolve() == local.resolve() and str(local).startswith(str(run))
    assert (local / 'session.json').read_text() == '{"workspaces": ["assistant"]}'
    assert not (local / 'herdr.sock').exists()
    assert (home / '.config/herdr/.plugins.lock').is_symlink()

    (local / 'session.json').write_text('{"workspaces": ["assistant", "more"]}')
    sup.save_herdr_state(local)
    (local / 'session.json').write_text('{"half-writ')          # mid-write: must not replace the saved copy
    sup.save_herdr_state(local)
    saved = home / '.local/state/herdr-runtime/herdr-session/session.json'
    assert json.loads(saved.read_text()) == {'workspaces': ['assistant', 'more']}

    import shutil
    shutil.rmtree(run)                                       # Pod restart: emptyDir is gone, the volume is not
    local = sup.prepare_herdr_session()
    assert json.loads((local / 'session.json').read_text()) == {'workspaces': ['assistant', 'more']}
    assert (local / 'session-snapshots/s1.json').is_file()


def test_baked_skill_links_are_retired_and_nothing_else(tmp_path, monkeypatch):
    # k8s#75: skills come from mouse9527/skills via bin/skills-project; links to the old baked copies go away
    monkeypatch.setattr(sup, 'HOME', tmp_path)
    for root in ('.claude/skills', '.codex/skills', '.pi/agent/skills'):
        d = tmp_path / root
        d.mkdir(parents=True)
        (d / 'workspace').symlink_to('/opt/herdr-runtime/skills/workspace')
        (d / 'mine').symlink_to('/somewhere/else')
    (tmp_path / '.codex/skills/.system').mkdir()
    sup.retire_baked_skill_links()
    for root in ('.claude/skills', '.codex/skills', '.pi/agent/skills'):
        assert sorted(p.name for p in (tmp_path / root).iterdir() if not p.name.startswith('.')) == ['mine']
    assert (tmp_path / '.codex/skills/.system').is_dir()


def test_skills_child_links_both_client_dirs(monkeypatch):
    monkeypatch.setenv('SKILLS_GROUPS', 'dev,assistant')
    argv = sup.skills_child().argv
    assert argv[1].endswith('bin/skills-project') and argv[2] == 'release'
    assert argv[argv.index('--groups') + 1] == 'dev,assistant'
    assert [argv[i + 1] for i, a in enumerate(argv) if a == '--link'] == [str(d) for d in sup.SKILL_DIRS]
    assert (OPT / 'bin/skills-project').stat().st_mode & 0o111


# ---- k8s#64 AC6: gateway key file + compiled MCP configs ----

def test_gateway_key_file_is_exported_to_children(tmp_path, monkeypatch):
    f = tmp_path / 'api-key'
    monkeypatch.setattr(sup, 'GATEWAY_KEY_FILE', f)
    monkeypatch.delenv('LITELLM_API_KEY', raising=False)
    assert sup.load_gateway_key() is False and 'LITELLM_API_KEY' not in sup.os.environ
    f.write_text('two\nlines\n')
    assert sup.load_gateway_key() is False
    f.write_text('sk-test-0123\n')
    assert sup.load_gateway_key() is True
    assert sup.os.environ['LITELLM_API_KEY'] == sup.os.environ['ANTHROPIC_AUTH_TOKEN'] == 'sk-test-0123'


def _mcp_dir(tmp_path):
    d = tmp_path / 'mcp'
    d.mkdir()
    entry = {'type': 'http', 'url': 'https://gw/mcp', 'headers': {'Authorization': 'Bearer ${LITELLM_API_KEY}'}}
    (d / 'claude.mcp.json').write_text(json.dumps({'mcpServers': {'litellm': entry}}))
    (d / 'pi.mcp.json').write_text(json.dumps({'mcpServers': {'litellm': {'url': 'https://gw/mcp'}}}))
    (d / 'codex.mcp.toml').write_text('[mcp_servers.litellm]\nurl = "https://gw/mcp"\nbearer_token_env_var = "LITELLM_API_KEY"\n')
    return d, entry


def test_mcp_configs_are_merged_and_the_rest_is_kept(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    (home / '.codex').mkdir(parents=True)
    d, entry = _mcp_dir(tmp_path)
    monkeypatch.setattr(sup, 'HOME', home)
    monkeypatch.setattr(sup, 'MCP_CONFIG_DIR', d)
    (home / '.claude.json').write_text(json.dumps({'numStartups': 7, 'mcpServers': {'mine': {'url': 'x'}, 'litellm': {'url': 'old'}}}))
    (home / '.codex/config.toml').write_text('model = "gpt"\n\n[mcp_servers.litellm]\nurl = "old"\n\n[mcp_servers.litellm.env]\nA = "b"\n\n'
                                             '[mcp_servers.mine]\nurl = "keep"\n\n[tui]\nx = true\n')
    sup.merge_mcp_configs()
    claude = json.loads((home / '.claude.json').read_text())
    assert claude['numStartups'] == 7 and claude['mcpServers'] == {'mine': {'url': 'x'}, 'litellm': entry}
    assert json.loads((home / '.pi/agent/mcp.json').read_text()) == {'mcpServers': {'litellm': {'url': 'https://gw/mcp'}}}
    codex = (home / '.codex/config.toml').read_text()
    assert 'url = "old"' not in codex and 'A = "b"' not in codex and codex.count('[mcp_servers.litellm]') == 1
    assert 'model = "gpt"' in codex and '[mcp_servers.mine]\nurl = "keep"' in codex and '[tui]\nx = true' in codex
    import tomllib
    assert tomllib.loads(codex)['mcp_servers']['litellm']['bearer_token_env_var'] == 'LITELLM_API_KEY'
    before = {p: p.read_text() for p in (home / '.claude.json', home / '.codex/config.toml', home / '.pi/agent/mcp.json')}
    sup.merge_mcp_configs()  # idempotent
    assert {p: p.read_text() for p in before} == before


def test_no_mcp_dir_or_broken_file_changes_nothing(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setattr(sup, 'HOME', home)
    monkeypatch.setattr(sup, 'MCP_CONFIG_DIR', tmp_path / 'absent')
    sup.merge_mcp_configs()
    d, _ = _mcp_dir(tmp_path)
    (d / 'claude.mcp.json').write_text('{broken')
    monkeypatch.setattr(sup, 'MCP_CONFIG_DIR', d)
    sup.merge_mcp_configs()  # logs, does not raise
    assert not (home / '.claude.json').exists() and (home / '.pi/agent/mcp.json').exists()
