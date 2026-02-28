"""
Code execution sandbox — runs untrusted code from agents in a restricted subprocess.

Supports: python, pytest, bash
Workspace is mounted read-only; PYTHONPATH points to its root.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

WORKSPACE_ROOT = pathlib.Path(os.environ.get("WORKSPACE_ROOT", "/workspace"))
MAX_OUTPUT = 12_000
TIMEOUT_DEFAULT = 30

app = FastAPI(title="Team Claw Sandbox")


class ExecuteRequest(BaseModel):
    code: str | None = None          # inline code to run
    file_path: str | None = None     # OR run an existing workspace file
    language: Literal["python", "bash", "pytest"] = "python"
    working_directory: str = ""
    timeout_seconds: int = TIMEOUT_DEFAULT


class ExecuteResult(BaseModel):
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    summary: str


@app.post("/execute", response_model=ExecuteResult)
def execute(req: ExecuteRequest) -> ExecuteResult:
    cwd = _resolve_cwd(req.working_directory)
    env = _build_env()

    try:
        cmd = _build_cmd(req)
    except ValueError as exc:
        return ExecuteResult(stdout="", stderr=str(exc), exit_code=1, timed_out=False, summary="❌ Bad request")

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=req.timeout_seconds,
            cwd=str(cwd),
            env=env,
        )
        ok = proc.returncode == 0
        return ExecuteResult(
            stdout=proc.stdout[:MAX_OUTPUT],
            stderr=proc.stderr[:MAX_OUTPUT],
            exit_code=proc.returncode,
            timed_out=False,
            summary="✅ Success" if ok else f"❌ Failed (exit {proc.returncode})",
        )
    except subprocess.TimeoutExpired:
        return ExecuteResult(
            stdout="",
            stderr=f"Timed out after {req.timeout_seconds}s",
            exit_code=124,
            timed_out=True,
            summary=f"⏱ Timed out ({req.timeout_seconds}s)",
        )
    except Exception as exc:
        return ExecuteResult(stdout="", stderr=str(exc), exit_code=1, timed_out=False, summary=f"❌ Error: {exc}")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "workspace": str(WORKSPACE_ROOT), "workspace_exists": WORKSPACE_ROOT.exists()}


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _resolve_cwd(directory: str) -> pathlib.Path:
    if not directory:
        return WORKSPACE_ROOT
    candidate = (WORKSPACE_ROOT / directory).resolve()
    if not str(candidate).startswith(str(WORKSPACE_ROOT)):
        return WORKSPACE_ROOT
    return candidate if candidate.exists() else WORKSPACE_ROOT


def _build_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(WORKSPACE_ROOT)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _build_cmd(req: ExecuteRequest) -> list[str]:
    if req.language == "python":
        if req.file_path:
            return ["python3", str(_safe_path(req.file_path))]
        if req.code:
            return ["python3", "-c", req.code]
        raise ValueError("Provide 'code' or 'file_path' for python.")

    if req.language == "pytest":
        target = req.file_path or "tests/"
        return ["python3", "-m", "pytest", str(_safe_path(target)), "-v", "--tb=short"]

    if req.language == "bash":
        if req.code:
            return ["bash", "-c", req.code]
        raise ValueError("Provide 'code' for bash.")

    raise ValueError(f"Unsupported language: {req.language}")


def _safe_path(raw: str) -> pathlib.Path:
    clean = pathlib.Path(raw)
    parts = clean.parts
    if parts and parts[0] in ("/", "workspace"):
        clean = pathlib.Path(*parts[1:])
    resolved = (WORKSPACE_ROOT / clean).resolve()
    if not str(resolved).startswith(str(WORKSPACE_ROOT)):
        raise ValueError(f"Path escapes workspace: {resolved}")
    return resolved
