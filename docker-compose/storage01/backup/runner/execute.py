"""Running things, and refusing to believe a dump that did not finish."""

from __future__ import annotations

import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .errors import JobError

PART_SUFFIX = ".part"


@dataclass(frozen=True)
class Result:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run(argv: list[str], *, input: bytes | None = None, timeout: int = 600) -> Result:
    proc = subprocess.run(
        argv, input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout
    )
    return Result(
        argv=argv,
        returncode=proc.returncode,
        stdout=proc.stdout.decode(errors="replace"),
        stderr=proc.stderr.decode(errors="replace"),
    )


def check(argv: list[str], what: str, **kwargs) -> Result:
    result = run(argv, **kwargs)
    if not result.ok:
        raise JobError(f"{what} failed (exit {result.returncode}): {result.stderr.strip()}")
    return result


def stream_to_file(argv: list[str], dest: Path, *, compress: bool, timeout: int = 7200) -> int:
    """Run `argv` and capture its stdout into `dest`, optionally gzipping.

    Nothing lands at `dest` until every exit code has been checked. A crashed
    run leaves a `.part` file, never a plausible-looking dump — the difference
    between a backup you can trust and one you find out about at restore time.

    The gzip runs here rather than on the far side on purpose: this process
    owns both ends of the pipe, so a producer that dies halfway is a nonzero
    exit code we see, not a well-formed gzip of a truncated stream.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.parent / (dest.name + PART_SUFFIX)
    part.unlink(missing_ok=True)
    try:
        return _stream(argv, dest, part, compress=compress, timeout=timeout)
    except BaseException:
        # A timeout or a kill must not leave a partial file that a later run
        # could mistake for a finished one.
        part.unlink(missing_ok=True)
        raise


def _stream(argv: list[str], dest: Path, part: Path, *, compress: bool, timeout: int) -> int:
    # Even when nothing needs compressing the bytes go through a second
    # process rather than a read loop in here, so that a producer which stalls
    # mid-stream hits `wait(timeout=)` instead of blocking the runner — and its
    # lock — forever.
    writer = ["gzip", "-c"] if compress else ["cat"]

    with tempfile.TemporaryFile() as errors, part.open("wb") as sink:
        producer = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=errors)
        consumer = None
        try:
            consumer = subprocess.Popen(
                writer, stdin=producer.stdout, stdout=sink, stderr=errors
            )
            # Only the consumer should hold the read end, so the producer sees
            # EPIPE if the consumer dies.
            producer.stdout.close()
            consumer.wait(timeout=timeout)
            producer.wait(timeout=timeout)
        finally:
            for process in (producer, consumer):
                if process is not None and process.poll() is None:
                    process.kill()

        errors.seek(0)
        stderr = errors.read().decode(errors="replace").strip()

    failures = [f"{shlex.join(argv[:3])}… exited {producer.returncode}"] if producer.returncode else []
    if consumer is not None and consumer.returncode:
        failures.append(f"{writer[0]} exited {consumer.returncode}")
    if failures:
        part.unlink(missing_ok=True)
        raise JobError(f"dump failed: {'; '.join(failures)}: {stderr}")

    size = part.stat().st_size
    if size == 0:
        part.unlink(missing_ok=True)
        raise JobError(f"dump produced an empty file: {stderr}")

    if compress:
        verify = run(["gzip", "-t", str(part)])
        if not verify.ok:
            part.unlink(missing_ok=True)
            raise JobError(f"the dump is not a valid gzip stream: {verify.stderr.strip()}")

    part.replace(dest)
    return size


def tree_size(path: Path) -> tuple[int, int]:
    """(bytes, files) actually present at the destination."""
    total = files = 0
    for entry in path.rglob("*"):
        if entry.is_file() and not entry.is_symlink():
            total += entry.stat().st_size
            files += 1
    return total, files
