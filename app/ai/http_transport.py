from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time

MAX_PROVIDER_RESPONSE_BYTES = 512_000
POLL_SECONDS = 0.05


def _curl_quote(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)


def post_json(
    *, url: str, headers: dict, payload: dict, timeout: float, cancel_check,
    no_proxy: bool = False,
) -> dict:
    """POST once in a killable curl process with a monotonic total deadline."""
    total = min(max(float(timeout), 0.05), 60.0)
    deadline = time.monotonic() + total

    def checkpoint() -> float:
        if cancel_check():
            raise InterruptedError("assistant run cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("model provider total deadline exceeded")
        return remaining

    curl = shutil.which("curl")
    if not curl:
        raise RuntimeError("curl is required for cancellable model requests")
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    config = [
        f'url = "{_curl_quote(url)}"',
        'request = "POST"',
        'silent',
        'show-error',
        'fail',
        f'max-time = {total:.3f}',
        f'connect-timeout = {min(total, 10.0):.3f}',
        'header = "Content-Type: application/json"',
    ]
    for key, value in headers.items():
        config.append(f'header = "{_curl_quote(str(key))}: {_curl_quote(str(value))}"')
    if no_proxy:
        config.extend(('proxy = ""', 'noproxy = "*"'))
    config.append(f'data-binary = "{_curl_quote(body)}"')

    process = subprocess.Popen(
        # -q must be curl's first argument; otherwise ~/.curlrc may enable
        # tracing or mutate the request and persist credentials/source text.
        [curl, "-q", "--config", "-"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    output = bytearray()
    overflow = threading.Event()

    def drain_output() -> None:
        while True:
            chunk = process.stdout.read(64 * 1024)
            if not chunk:
                return
            output.extend(chunk)
            if len(output) > MAX_PROVIDER_RESPONSE_BYTES:
                overflow.set()
                _terminate(process)
                return

    reader = threading.Thread(target=drain_output, daemon=True)
    reader.start()
    try:
        process.stdin.write(("\n".join(config) + "\n").encode("utf-8"))
        process.stdin.close()
        while process.poll() is None:
            checkpoint()
            time.sleep(POLL_SECONDS)
        checkpoint()
        reader.join(timeout=1)
        if overflow.is_set():
            raise RuntimeError("model provider response too large")
        if process.returncode != 0:
            raise RuntimeError(f"model provider request failed ({process.returncode})")
        raw = bytes(output)
    except (InterruptedError, TimeoutError):
        _terminate(process)
        reader.join(timeout=1)
        raise
    finally:
        _terminate(process)
        if process.stdout is not None:
            process.stdout.close()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as error:
        raise RuntimeError("model provider returned invalid JSON") from error
    if not isinstance(value, dict):
        raise RuntimeError("model provider returned invalid response")
    return value
