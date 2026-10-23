"""Code tools for real repositories: paged reads, regex search, exact edits, and string test commands.

Seen live: tool results are capped, a 19k-character test file could never be read past the cap, the engineer re-read
it in a loop and reported it could not edit it (repo_write needs the whole file). And a planner wrote the sandbox
command as one string, which ran as a program named "python -m pytest ..." (exit 127)."""
import pytest

from nexus.appctx import build_context
from nexus.secrets import MemorySecretStore
from nexus.tools.broker import ToolContext
from nexus.tools.builtin import READ_PAGE_CHARS
from nexus.tools.sandbox import as_argv
from nexus.tools.sandbox import UnavailableSandbox


@pytest.fixture
def env(tmp_path):
    c = build_context(tmp_path / "d", secrets=MemorySecretStore(), sandbox=UnavailableSandbox("test"))
    wt = tmp_path / "wt"
    (wt / "pkg").mkdir(parents=True)
    big = "\n".join(f"def f{i}():\n    return {i}\n" for i in range(800))  # ~ 20k characters
    (wt / "pkg" / "big.py").write_text(big, encoding="utf-8")
    (wt / "pkg" / "crlf.py").write_bytes(b"a = 1\r\nb = 2\r\nc = 3\r\n")
    allowed = c.broker.capability_bundles["repo_read"] | c.broker.capability_bundles["repo_write"]
    ctx = ToolContext(project_id="p", workspace_root=wt, agent_id="agent:t", allowed_tools=allowed, worktree=wt)
    yield c, ctx, wt
    c.close()


async def call(c, ctx, name, args):
    r = await c.broker.invoke(ctx, name, args, operation_id=f"op-{name}-{hash(str(args))}")
    return r


async def test_large_files_are_read_in_pages_to_the_end(env):
    c, ctx, wt = env
    r = (await call(c, ctx, "repo_read", {"path": "pkg/big.py"}))["result"]
    assert r["start_line"] == 1 and r["total_lines"] > 1600 and "next_start_line" in r
    assert len(r["content"]) <= READ_PAGE_CHARS
    seen, nxt = [r["content"]], r.get("next_start_line")
    while nxt:
        r = (await call(c, ctx, "repo_read", {"path": "pkg/big.py", "start_line": nxt}))["result"]
        seen.append(r["content"])
        nxt = r.get("next_start_line")
    assert "\n".join(seen) == (wt / "pkg" / "big.py").read_text(encoding="utf-8").rstrip("\n")
    part = (await call(c, ctx, "repo_read", {"path": "pkg/big.py", "start_line": 5, "end_line": 6}))["result"]
    assert part["content"] == "    return 1\n" and part["end_line"] == 6 and "next_start_line" not in part


async def test_search_finds_lines_and_edit_changes_exactly_one_place(env):
    c, ctx, wt = env
    m = (await call(c, ctx, "repo_search", {"pattern": r"def f79\d\(", "path_glob": "pkg/*.py"}))["result"]["matches"]
    assert [x["text"] for x in m] == [f"def f79{i}():" for i in range(10)]
    r = await call(c, ctx, "repo_edit", {"path": "pkg/big.py", "old_string": "def f799():\n    return 799",
                                         "new_string": "def f799():\n    return -1"})
    assert r["ok"] and r["result"]["replacements"] == 1
    assert "return -1" in (wt / "pkg" / "big.py").read_text(encoding="utf-8")
    amb = await call(c, ctx, "repo_edit", {"path": "pkg/big.py", "old_string": "return", "new_string": "yield"})
    assert not amb["ok"] and "occurs" in amb["error"]
    missing = await call(c, ctx, "repo_edit", {"path": "pkg/big.py", "old_string": "nope", "new_string": "x"})
    assert not missing["ok"] and "not found" in missing["error"]


async def test_edit_keeps_windows_line_endings(env):
    c, ctx, wt = env
    r = await call(c, ctx, "repo_edit", {"path": "pkg/crlf.py", "old_string": "b = 2", "new_string": "b = 20"})
    assert r["ok"]
    assert (wt / "pkg" / "crlf.py").read_bytes() == b"a = 1\r\nb = 20\r\nc = 3\r\n"


def test_string_commands_are_split_like_a_shell():
    assert as_argv("python -m pytest -q -p no:cacheprovider tests/service") == \
        ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/service"]
    assert as_argv(["python", "-V"]) == ["python", "-V"]
    with pytest.raises(ValueError):
        as_argv(42)
