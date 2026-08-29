from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

from connect_hub.contracts import ConnectJobError, JobErrorCode


class CancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise ConnectJobError(
                JobErrorCode.JOB_CANCELLED,
                "任务已由用户取消。",
                technical_message="job cancellation requested",
            )


@dataclass(frozen=True)
class ProcessResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    pid: int | None = None


@dataclass
class ManagedProcessRunner:
    cancel_grace_seconds: float = 3.0
    poll_seconds: float = 0.2
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False)
    _processes: dict[str, subprocess.Popen[str]] = field(default_factory=dict, init=False)

    def run(
        self,
        command: Sequence[str],
        *,
        job_id: str,
        cancellation: CancellationToken,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        text: bool = True,
        capture_output: bool = True,
        on_start: Callable[[int, str], None] | None = None,
        on_finish: Callable[[int], None] | None = None,
    ) -> ProcessResult:
        if not command:
            raise ValueError("command cannot be empty")
        cancellation.raise_if_cancelled()
        normalized = tuple(str(item) for item in command)
        popen_kwargs: dict[str, object] = {
            "cwd": str(cwd) if cwd is not None else None,
            "env": dict(env) if env is not None else None,
            "text": text,
            "shell": False,
        }
        if capture_output:
            popen_kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        process = subprocess.Popen(normalized, **popen_kwargs)  # type: ignore[arg-type]
        with self._lock:
            self._processes[job_id] = process
        try:
            if on_start is not None:
                on_start(process.pid, normalized[0])
        except Exception:
            self._terminate_tree(process)
            with self._lock:
                self._processes.pop(job_id, None)
            if on_finish is not None:
                on_finish(process.pid)
            raise
        started = time.monotonic()
        try:
            while True:
                if cancellation.cancelled:
                    self._terminate_tree(process)
                    raise ConnectJobError(
                        JobErrorCode.JOB_CANCELLED,
                        "任务已由用户取消。",
                        technical_message=f"cancelled process tree pid={process.pid}",
                    )
                if timeout is not None and time.monotonic() - started >= timeout:
                    self._terminate_tree(process)
                    raise ConnectJobError(
                        JobErrorCode.JOB_TIMEOUT,
                        "任务执行超时，相关子进程已终止。",
                        retryable=True,
                        technical_message=f"process tree pid={process.pid} exceeded {timeout}s",
                    )
                try:
                    stdout, stderr = process.communicate(timeout=self.poll_seconds)
                    return ProcessResult(
                        args=normalized,
                        returncode=int(process.returncode or 0),
                        stdout=stdout or "",
                        stderr=stderr or "",
                        pid=process.pid,
                    )
                except subprocess.TimeoutExpired:
                    continue
        finally:
            with self._lock:
                self._processes.pop(job_id, None)
            if on_finish is not None:
                on_finish(process.pid)

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            process = self._processes.get(job_id)
        if process is None or process.poll() is not None:
            return False
        self._terminate_tree(process)
        return True

    def active_pid(self, job_id: str) -> int | None:
        with self._lock:
            process = self._processes.get(job_id)
        if process is None or process.poll() is not None:
            return None
        return process.pid

    def terminate_pid_tree(self, pid: int, *, expected_executable: str = "") -> bool:
        """Terminate a process tree recorded by a previous Hub process.

        On Linux the stored executable is compared with /proc before signalling.
        This prevents an old, reused PID from targeting an unrelated process.
        """
        if pid <= 0 or pid == os.getpid() or not _pid_exists(pid):
            return False
        if expected_executable and not _pid_matches_executable(pid, expected_executable):
            return False
        if os.name == "nt":
            try:
                completed = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=self.cancel_grace_seconds,
                )
                return completed.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                return False

        try:
            process_group = os.getpgid(pid)
        except (ProcessLookupError, PermissionError):
            return False
        # ManagedProcessRunner always creates a new session. Refuse to signal
        # an inherited process group because it could contain unrelated work.
        if process_group != pid:
            return False
        try:
            os.killpg(process_group, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return False
        deadline = time.monotonic() + self.cancel_grace_seconds
        while time.monotonic() < deadline:
            if not _pid_exists(pid):
                return True
            time.sleep(min(self.poll_seconds, 0.1))
        try:
            os.killpg(process_group, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        return True

    def _terminate_tree(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            self._terminate_windows_tree(process)
            return
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                process.terminate()
            except ProcessLookupError:
                return
        try:
            process.wait(timeout=self.cancel_grace_seconds)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                process.kill()
            except ProcessLookupError:
                return
        try:
            process.wait(timeout=self.cancel_grace_seconds)
        except subprocess.TimeoutExpired:
            pass

    def _terminate_windows_tree(self, process: subprocess.Popen[str]) -> None:
        # taskkill /T is the standard Windows mechanism for terminating the
        # full descendant tree. It is invoked without a shell, so arguments
        # cannot be interpreted as commands.
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=self.cancel_grace_seconds,
            )
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except ProcessLookupError:
                return
        try:
            process.wait(timeout=self.cancel_grace_seconds)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except ProcessLookupError:
                pass


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_matches_executable(pid: int, expected: str) -> bool:
    if os.name == "nt":
        # Windows has no dependency-free equivalent of /proc cmdline. The PID
        # still comes only from an active job written by this installation.
        return True
    cmdline = Path(f"/proc/{pid}/cmdline")
    try:
        actual = cmdline.read_bytes().split(b"\0", 1)[0].decode(errors="replace")
        if not actual:
            return False
        return Path(actual).resolve() == Path(expected).resolve()
    except (OSError, RuntimeError):
        return False
