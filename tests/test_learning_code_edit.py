"""Tests for the sandboxed write/commit layer of training mode."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from studiomind.learning import code_edit as ce
from studiomind.learning.approval_tokens import ApprovalError, ApprovalStore
from studiomind.learning.sandbox import SandboxViolation


# ───────────────────────────── fixtures ───────────────────────────────

@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """Build a faux repo layout with src/studiomind/skills/ so the
    sandbox can resolve writeable paths under it."""
    repo = tmp_path / "repo"
    (repo / "src" / "studiomind" / "skills").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname='test'\n")
    return repo


@pytest.fixture
def store() -> ApprovalStore:
    return ApprovalStore(ttl_seconds=600.0)


@pytest.fixture
def queue(fake_repo: Path) -> ce.WriteQueue:
    return ce.WriteQueue(current_skill="test_skill", repo_root=fake_repo)


# Helper that monkeypatches the sandbox's REPO_ROOT/SKILLS_DIR so
# assert_path_writable accepts paths under our test fake_repo.
@pytest.fixture(autouse=True)
def _patch_sandbox_repo_root(monkeypatch, fake_repo: Path) -> None:
    from studiomind.learning import sandbox as sb
    monkeypatch.setattr(sb, "REPO_ROOT", fake_repo.resolve())
    monkeypatch.setattr(
        sb, "SKILLS_DIR",
        (fake_repo / "src" / "studiomind" / "skills").resolve(),
    )


# ───────────────────────────── propose / apply writes ────────────────

def test_propose_write_queues_without_disk_io(queue: ce.WriteQueue) -> None:
    pw = ce.propose_write(
        queue,
        rel_path="src/studiomind/skills/test_skill/wrapper.py",
        content="VERSION = 1\n",
    )
    assert isinstance(pw, ce.PendingWrite)
    assert len(queue.writes) == 1
    # Disk untouched
    assert not pw.abs_path.exists()


def test_propose_write_outside_skill_dir_rejected(queue: ce.WriteQueue) -> None:
    with pytest.raises(SandboxViolation):
        ce.propose_write(
            queue,
            rel_path="src/studiomind/skills/other_skill/wrapper.py",
            content="x = 1",
        )


def test_propose_write_outside_repo_rejected(queue: ce.WriteQueue, tmp_path: Path) -> None:
    with pytest.raises(SandboxViolation):
        ce.propose_write(
            queue,
            rel_path="../escape.py",
            content="x = 1",
        )


def test_payload_is_sorted_by_path(queue: ce.WriteQueue) -> None:
    """Reordering proposals must NOT invalidate the approval token —
    the payload sorts by rel_path so the hash is stable."""
    ce.propose_write(queue, rel_path="src/studiomind/skills/test_skill/z.py", content="z")
    ce.propose_write(queue, rel_path="src/studiomind/skills/test_skill/a.py", content="a")
    paths = [entry["path"] for entry in queue.to_payload()]
    assert paths == sorted(paths)


def test_apply_proposed_writes_flushes_to_disk(
    queue: ce.WriteQueue, store: ApprovalStore,
) -> None:
    ce.propose_write(
        queue, rel_path="src/studiomind/skills/test_skill/wrapper.py",
        content="VERSION = 1\n",
    )
    ce.propose_write(
        queue, rel_path="src/studiomind/skills/test_skill/manifest.json",
        content="{}\n",
    )

    token = ce.request_writes_approval(queue, store)
    written = ce.apply_proposed_writes(queue, token=token, approval_store=store)

    assert len(written) == 2
    for path in written:
        assert path.exists()
    assert queue.writes == []   # cleared after apply


def test_apply_proposed_writes_clears_queue(queue, store) -> None:
    ce.propose_write(
        queue, rel_path="src/studiomind/skills/test_skill/x.py", content="x = 1",
    )
    token = ce.request_writes_approval(queue, store)
    ce.apply_proposed_writes(queue, token=token, approval_store=store)
    # A second apply must fail (token consumed; queue empty payload
    # wouldn't match anyway).
    with pytest.raises(ApprovalError):
        ce.apply_proposed_writes(queue, token=token, approval_store=store)


def test_apply_without_token_rejected(queue, store) -> None:
    ce.propose_write(
        queue, rel_path="src/studiomind/skills/test_skill/x.py", content="x = 1",
    )
    with pytest.raises(ApprovalError):
        ce.apply_proposed_writes(queue, token="bogus", approval_store=store)
    # Disk untouched on rejection.
    assert not queue.writes[0].abs_path.exists()


def test_apply_with_mutated_payload_rejected(queue, store) -> None:
    """If the agent mutates the queue between propose-token and apply,
    the token's payload hash no longer matches."""
    ce.propose_write(
        queue, rel_path="src/studiomind/skills/test_skill/x.py", content="x = 1",
    )
    token = ce.request_writes_approval(queue, store)
    # Mutate after the token is issued.
    ce.propose_write(
        queue, rel_path="src/studiomind/skills/test_skill/y.py", content="y = 2",
    )
    with pytest.raises(ApprovalError):
        ce.apply_proposed_writes(queue, token=token, approval_store=store)


# ───────────────────────────── commit message + trailers ──────────────

def test_build_commit_message_appends_trailers(fake_repo: Path) -> None:
    proposal = ce.CommitProposal(
        current_skill="test_skill",
        repo_root=fake_repo,
        message="Acquire test_skill via training mode",
        paths=["src/studiomind/skills/test_skill/wrapper.py"],
        trailers={
            "Skill-Acquired-Via": "studiomind-training-mode",
            "Skill-Name": "test_skill",
            "FL-Version": "21.2.10",
        },
    )
    msg = ce.build_commit_message(proposal)
    assert "Acquire test_skill via training mode" in msg
    # Trailers separated by a blank line, sorted alphabetically.
    lines = msg.strip().splitlines()
    assert "FL-Version: 21.2.10" in lines
    assert "Skill-Acquired-Via: studiomind-training-mode" in lines
    assert "Skill-Name: test_skill" in lines
    # Ordering: FL-Version comes before Skill-* by sort.
    assert lines.index("FL-Version: 21.2.10") < lines.index("Skill-Acquired-Via: studiomind-training-mode")


def test_build_commit_message_without_trailers(fake_repo: Path) -> None:
    proposal = ce.CommitProposal(
        current_skill="test_skill", repo_root=fake_repo,
        message="Hello", paths=[],
    )
    assert ce.build_commit_message(proposal) == "Hello\n"


# ───────────────────────────── apply_commit (real git) ────────────────

@pytest.fixture
def git_repo(fake_repo: Path) -> Path:
    """Initialise an actual git repo at fake_repo so apply_commit can
    do real git add/commit. Configures user.name/user.email locally
    so commits succeed in CI environments without global config."""
    subprocess.run(["git", "init", "-q"], cwd=fake_repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=fake_repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=fake_repo, check=True)
    # Initial commit so HEAD exists.
    (fake_repo / "README.md").write_text("# test\n")
    subprocess.run(["git", "add", "README.md"], cwd=fake_repo, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"],
        cwd=fake_repo, check=True,
    )
    return fake_repo


def test_apply_commit_creates_commit(
    git_repo: Path, queue: ce.WriteQueue, store: ApprovalStore,
) -> None:
    # Stage a write through the queue first.
    ce.propose_write(
        queue, rel_path="src/studiomind/skills/test_skill/wrapper.py",
        content="x = 1\n",
    )
    write_token = ce.request_writes_approval(queue, store)
    ce.apply_proposed_writes(queue, token=write_token, approval_store=store)

    # Now commit.
    proposal = ce.CommitProposal(
        current_skill="test_skill",
        repo_root=git_repo,
        message="Acquire test_skill",
        paths=["src/studiomind/skills/test_skill/wrapper.py"],
        trailers={"Skill-Acquired-Via": "studiomind-training-mode"},
    )
    commit_token = ce.request_commit_approval(proposal, store)
    sha = ce.apply_commit(proposal, token=commit_token, approval_store=store)
    assert len(sha) == 40

    # Verify the trailer landed in the commit message.
    msg = subprocess.run(
        ["git", "log", "-1", "--format=%B", sha],
        cwd=git_repo, capture_output=True, text=True, check=True,
    ).stdout
    assert "Skill-Acquired-Via: studiomind-training-mode" in msg


def test_apply_commit_without_token_rejected(
    git_repo: Path, store: ApprovalStore,
) -> None:
    proposal = ce.CommitProposal(
        current_skill="test_skill",
        repo_root=git_repo,
        message="X",
        paths=[],
    )
    with pytest.raises(ApprovalError):
        ce.apply_commit(proposal, token="bogus", approval_store=store)


def test_apply_commit_rejects_paths_outside_skill(
    git_repo: Path, store: ApprovalStore,
) -> None:
    proposal = ce.CommitProposal(
        current_skill="test_skill",
        repo_root=git_repo,
        message="X",
        paths=["src/studiomind/skills/other_skill/wrapper.py"],   # not in the current skill
    )
    token = ce.request_commit_approval(proposal, store)
    with pytest.raises(SandboxViolation):
        ce.apply_commit(proposal, token=token, approval_store=store)


def test_apply_commit_rejects_push_through_runner(
    git_repo: Path, store: ApprovalStore, queue, monkeypatch,
) -> None:
    """Even if a tampered runner tried to slip a 'git push' into the
    arglist, the sandbox check happens BEFORE the runner fires. We
    can't smuggle in a different command via the runner kwarg."""
    ce.propose_write(
        queue, rel_path="src/studiomind/skills/test_skill/wrapper.py", content="x",
    )
    wt = ce.request_writes_approval(queue, store)
    ce.apply_proposed_writes(queue, token=wt, approval_store=store)

    proposal = ce.CommitProposal(
        current_skill="test_skill",
        repo_root=git_repo,
        message="X",
        paths=["src/studiomind/skills/test_skill/wrapper.py"],
    )
    token = ce.request_commit_approval(proposal, store)

    # Inject a runner that tries to push — but apply_commit always
    # asks the runner only for `git add`, `git commit`, `git rev-parse`.
    received: list[list[str]] = []

    def runner(args, **kwargs):
        received.append(list(args))
        return subprocess.run(args, **kwargs)

    sha = ce.apply_commit(proposal, token=token, approval_store=store, runner=runner)
    # Confirm the runner only received allowlisted invocations.
    invoked = [a[1] for a in received if len(a) > 1]
    assert "push" not in invoked
    assert {"add", "commit", "rev-parse"} >= set(invoked)
    assert len(sha) == 40


# ───────────────────────────── read_repo_file ────────────────────────

def test_read_repo_file_reads_existing_file(fake_repo: Path) -> None:
    target = fake_repo / "README.md"
    target.write_text("hello\n")
    assert ce.read_repo_file(target) == "hello\n"


def test_read_repo_file_rejects_dotssh(tmp_path: Path) -> None:
    secret = tmp_path / ".ssh" / "id_rsa"
    secret.parent.mkdir(parents=True)
    secret.write_text("FAKE_KEY")
    with pytest.raises(SandboxViolation):
        ce.read_repo_file(secret)
