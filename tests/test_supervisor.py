import importlib.machinery, importlib.util
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

