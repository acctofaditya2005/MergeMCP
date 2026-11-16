import os

import pytest

from merge_mcp.config import Settings
from merge_mcp.workspace import WorkspaceError, WorkspaceManager, branch_name, slugify


@pytest.fixture
async def ws(settings, fixture_repo):
    manager = WorkspaceManager(settings)
    workspace = await manager.create(
        fixture_repo["path"],
        ref=fixture_repo["base"],
        issue_number=7,
        issue_title="slugify keeps punctuation!",
    )
    return manager, workspace


def test_branch_names():
    assert slugify("Crash: on  EMPTY input?") == "crash-on-empty-input"
    assert branch_name(7, "Crash on empty") == "fix/issue-7-crash-on-empty"
    assert branch_name(None, None) == "contrib/change"


async def test_create_checks_out_base_on_new_branch(ws, fixture_repo):
    manager, w = ws
    assert w.base_commit == fixture_repo["base"]
    assert w.branch == "fix/issue-7-slugify-keeps-punctuation"
    assert manager.get(w.workspace_id) == w
    assert [x.workspace_id for x in manager.list()] == [w.workspace_id]


async def test_local_sources_refused_by_default(tmp_path, fixture_repo):
    manager = WorkspaceManager(Settings(home=tmp_path / "h"))
    with pytest.raises(WorkspaceError, match="disabled"):
        await manager.create(fixture_repo["path"])
    with pytest.raises(ValueError):
        await manager.create("https://evil.example.com/a/b")


async def test_bad_ref_is_rejected_before_git_runs(settings, fixture_repo):
    with pytest.raises(WorkspaceError, match="Invalid git ref"):
        await WorkspaceManager(settings).create(fixture_repo["path"], ref="--upload-pack=x")


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", "~/x", "a/../../x", ""])
async def test_path_jail_blocks_escapes(ws, path):
    manager, w = ws
    with pytest.raises(WorkspaceError):
        manager.read_file(w.workspace_id, path)
    with pytest.raises(WorkspaceError):
        manager.write_file(w.workspace_id, path, "x")


async def test_path_jail_blocks_symlink_escape(ws, tmp_path):
    manager, w = ws
    secret = tmp_path / "secret.txt"
    secret.write_text("token")
    os.symlink(secret, manager.checkout(w.workspace_id) / "link.txt")
    with pytest.raises(WorkspaceError, match="escapes"):
        manager.read_file(w.workspace_id, "link.txt")


async def test_git_dir_and_venv_are_write_protected(ws):
    manager, w = ws
    for path in (".git/hooks/pre-commit", ".git/config", ".merge-mcp-venv/bin/python"):
        with pytest.raises(WorkspaceError, match="not allowed"):
            manager.write_file(w.workspace_id, path, "x")


async def test_unknown_or_malformed_workspace_id(settings):
    manager = WorkspaceManager(settings)
    for bad in ("ws_0000000000", "../../etc", "ws_zz"):
        with pytest.raises(WorkspaceError):
            manager.get(bad)


async def test_read_edit_search_diff_roundtrip(ws):
    manager, w = ws
    wid = w.workspace_id
    content = manager.read_file(wid, "textkit.py", start_line=4, max_lines=3)
    assert content.start_line == 4 and content.end_line == 6 and "def slugify" in content.content

    hits = await manager.search(wid, r"def \w+\(")
    assert {(m.path, m.text.split("(")[0]) for m in hits.matches} >= {
        ("textkit.py", "def slugify"),
        ("textkit.py", "def truncate"),
    }

    manager.edit_file(
        wid,
        "textkit.py",
        'return "-".join(text.lower().split())',
        'return "-".join(text.lower().replace(",", "").split())',
    )
    created = manager.write_file(wid, "docs/notes.md", "hello\n")
    assert created.created

    diff = await manager.diff(wid)
    assert diff.files_changed == ["docs/notes.md", "textkit.py"]  # new files are included
    assert (diff.insertions, diff.deletions) == (2, 1)
    assert "docs/notes.md" in (await manager.list_files(wid, "docs")).files


async def test_edit_requires_exactly_one_match(ws):
    manager, w = ws
    with pytest.raises(WorkspaceError, match="matched 0 times"):
        manager.edit_file(w.workspace_id, "textkit.py", "no such text", "x")
    with pytest.raises(WorkspaceError, match="matched 2 times"):
        manager.edit_file(w.workspace_id, "textkit.py", "def ", "async def ")


async def test_delete_removes_everything(ws):
    manager, w = ws
    manager.delete(w.workspace_id)
    assert manager.list() == []


async def test_delete_handles_read_only_cache_directories(ws):
    """Go's module cache, for one, is written read-only."""
    manager, w = ws
    cache = manager.checkout(w.workspace_id) / ".merge-mcp-cache" / "go" / "pkg"
    cache.mkdir(parents=True)
    (cache / "module.go").write_text("package x\n")
    os.chmod(cache / "module.go", 0o400)
    os.chmod(cache, 0o500)
    manager.delete(w.workspace_id)
    assert manager.list() == []
