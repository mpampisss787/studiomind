"""Sandboxed write/commit layer for the training agent.

Two-step gate, end-to-end:

  1. Agent calls ``propose_write(queue, rel_path, content)`` — sandbox
     validates that ``rel_path`` resolves under the current skill's
     directory; the entry lands in an in-memory queue. Nothing has
     touched disk yet.
  2. Agent calls ``request_writes_approval(queue, approval_store)``
     to mint a one-shot token bound to the queue's contents. The
     token + a payload preview go to the UI.
  3. User approves; the UI calls ``apply_proposed_writes(queue,
     token, approval_store)`` which validates the token and flushes
     every queued entry to disk (creating parent dirs).

Same shape for commits via ``propose_commit`` /
``request_commit_approval`` / ``apply_commit``. Commits go through
``sandbox.assert_safe_git`` and pick up a structured trailer so
``git log --grep='Skill-Acquired-Via'`` lists every training-mode
commit. **No push, ever** — the trailer is informational; the
sandbox would reject the push too.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from studiomind.learning.approval_tokens import ApprovalStore
from studiomind.learning.sandbox import (
    SandboxViolation,
    assert_path_readable,
    assert_path_writable,
    assert_safe_git,
)

log = logging.getLogger(__name__)


# ───────────────────────────── data classes ───────────────────────────

@dataclass(frozen=True)
class PendingWrite:
    """One queued write — path is the sandbox-validated absolute Path
    on disk; rel_path is the same path expressed under repo_root for
    display + payload hashing."""
    rel_path: str
    abs_path: Path
    content: str


@dataclass
class WriteQueue:
    """Accumulates proposed writes for one acquisition session.

    Each queue is bound to ``current_skill`` and ``repo_root``;
    ``propose_write`` rejects paths that don't resolve under
    ``<repo_root>/src/studiomind/skills/<current_skill>/``.
    """
    current_skill: str
    repo_root: Path
    writes: list[PendingWrite] = field(default_factory=list)

    def to_payload(self) -> list[dict[str, Any]]:
        """Stable representation used as the approval-token payload.
        Sorted by rel_path so reordering writes doesn't break the
        token binding."""
        return [
            {"path": w.rel_path, "content": w.content}
            for w in sorted(self.writes, key=lambda x: x.rel_path)
        ]


@dataclass
class CommitProposal:
    """A pending commit. ``trailers`` are appended verbatim to the
    message before the commit fires."""
    current_skill: str
    repo_root: Path
    message: str
    paths: list[str]                              # rel paths to stage
    trailers: dict[str, str] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "skill": self.current_skill,
            "message": self.message,
            "paths": sorted(self.paths),
            "trailers": dict(sorted(self.trailers.items())),
        }


# ───────────────────────────── read ───────────────────────────────────

def read_repo_file(path: str | Path, *, repo_root: Path | None = None) -> str:
    """Read-only access to repo files. Routes through sandbox so the
    training agent can't read ~/.ssh or ~/obsidian-vault.

    Note: ``assert_path_readable`` (in P2) enforces the
    inside-the-repo-tree rule but is otherwise permissive — read is
    less strict than write."""
    resolved = assert_path_readable(path)
    if repo_root is not None and repo_root not in resolved.parents and resolved != repo_root:
        # Extra belt: reject paths outside repo even if assert_path_readable
        # was permissive about something. (No-op when repo_root is in the
        # ancestor chain, which is the common case.)
        repo_resolved = repo_root.resolve()
        if repo_resolved not in resolved.parents and resolved != repo_resolved:
            raise SandboxViolation(
                f"Refusing to read {resolved} — outside repo_root {repo_resolved}"
            )
    return resolved.read_text()


# ───────────────────────────── propose / apply writes ────────────────

def propose_write(
    queue: WriteQueue,
    rel_path: str,
    content: str,
) -> PendingWrite:
    """Validate ``rel_path`` against the sandbox + the current skill,
    then append the (validated) entry to the queue. Disk is NOT
    touched until ``apply_proposed_writes`` runs."""
    abs_path = (queue.repo_root / rel_path).resolve()
    abs_path = assert_path_writable(abs_path, current_skill=queue.current_skill)
    pw = PendingWrite(rel_path=rel_path, abs_path=abs_path, content=content)
    queue.writes.append(pw)
    return pw


def request_writes_approval(
    queue: WriteQueue,
    approval_store: ApprovalStore,
) -> str:
    """Mint a one-shot approval token bound to the queue's payload."""
    return approval_store.issue("writes", queue.to_payload())


def apply_proposed_writes(
    queue: WriteQueue,
    *,
    token: str,
    approval_store: ApprovalStore,
) -> list[Path]:
    """Validate ``token`` for the current queue payload, then flush
    every pending write to disk. Returns the absolute paths written.

    The queue is cleared on success so the same approval can't
    accidentally apply twice."""
    approval_store.consume(token, "writes", queue.to_payload())
    written: list[Path] = []
    for w in queue.writes:
        # Re-validate the path on apply to catch the (rare) case where
        # the queue or skill identity has been mutated since propose.
        validated = assert_path_writable(w.abs_path, current_skill=queue.current_skill)
        validated.parent.mkdir(parents=True, exist_ok=True)
        validated.write_text(w.content)
        written.append(validated)
        log.info("Training mode wrote %s (%d bytes)", validated, len(w.content))
    queue.writes.clear()
    return written


# ───────────────────────────── commit ─────────────────────────────────

DEFAULT_TRAILER_KEY = "Skill-Acquired-Via"
DEFAULT_TRAILER_VALUE = "studiomind-training-mode"


def build_commit_message(proposal: CommitProposal) -> str:
    """Render the final commit message: ``proposal.message`` followed
    by a blank line and the structured trailers. Trailers are sorted
    by key for deterministic output."""
    body = proposal.message.rstrip()
    trailer_lines = [
        f"{k}: {v}" for k, v in sorted(proposal.trailers.items())
    ]
    if trailer_lines:
        return body + "\n\n" + "\n".join(trailer_lines) + "\n"
    return body + "\n"


def request_commit_approval(
    proposal: CommitProposal,
    approval_store: ApprovalStore,
) -> str:
    return approval_store.issue("commit", proposal.to_payload())


def apply_commit(
    proposal: CommitProposal,
    *,
    token: str,
    approval_store: ApprovalStore,
    runner: Any | None = None,
) -> str:
    """Validate ``token``, run ``git add <paths> && git commit -m
    <message>`` in the proposal's repo_root, return the new commit
    SHA. ``runner`` (default subprocess.run) is injectable for tests.

    Refuses to push, rebase, reset, or anything else — the only git
    invocations that ever fire are the two allowlisted ones."""
    approval_store.consume(token, "commit", proposal.to_payload())

    # Validate every path resolves under the current skill before we
    # let git near them. We work in absolutes so the sandbox's CWD-
    # relative resolution doesn't matter.
    abs_paths: list[str] = []
    for rel in proposal.paths:
        abs_path = (proposal.repo_root / rel).resolve()
        assert_path_writable(abs_path, current_skill=proposal.current_skill)
        abs_paths.append(str(abs_path))

    # Sandbox-allowlist the git args — assert_safe_git will raise
    # SandboxViolation on anything outside the expected shape. It
    # expects the args list WITHOUT the leading "git" token.
    sandbox_add = ["add"] + abs_paths
    assert_safe_git(sandbox_add, current_skill=proposal.current_skill)

    commit_message = build_commit_message(proposal)
    sandbox_commit = ["commit", "-m", commit_message]
    assert_safe_git(sandbox_commit, current_skill=proposal.current_skill)

    add_args = ["git"] + sandbox_add
    commit_args = ["git"] + sandbox_commit

    runner = runner or subprocess.run
    add_result = runner(add_args, cwd=proposal.repo_root, capture_output=True, text=True)
    if add_result.returncode != 0:
        raise RuntimeError(
            f"git add failed: {add_result.stderr.strip() or add_result.stdout.strip()}"
        )

    commit_result = runner(commit_args, cwd=proposal.repo_root, capture_output=True, text=True)
    if commit_result.returncode != 0:
        raise RuntimeError(
            f"git commit failed: {commit_result.stderr.strip() or commit_result.stdout.strip()}"
        )

    # Resolve the SHA we just produced.
    sha_result = runner(
        ["git", "rev-parse", "HEAD"],
        cwd=proposal.repo_root, capture_output=True, text=True,
    )
    if sha_result.returncode != 0:
        raise RuntimeError(
            f"git rev-parse HEAD failed: {sha_result.stderr.strip()}"
        )
    sha = sha_result.stdout.strip()
    log.info(
        "Training mode committed %s for skill %s",
        sha[:7], proposal.current_skill,
    )
    return sha
