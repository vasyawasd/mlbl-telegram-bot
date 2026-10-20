"""Reliable process supervisor for the MLBL Telegram bot."""

import atexit
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from threading import Event

SCRIPT_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = Path(os.environ.get("BOT_RUNTIME_DIR", SCRIPT_DIR))
LOCK_FILE = RUNTIME_DIR / ".bot.lock"
PID_FILE = RUNTIME_DIR / ".bot.pid"

EXIT_OK = 0
EXIT_CONFIG = 2
INITIAL_BACKOFF_SECONDS = 2.0
MAX_BACKOFF_SECONDS = 30.0
STABLE_RUN_SECONDS = 60.0
JITTER_MIN = 0.8
JITTER_MAX = 1.2
CHILD_SHUTDOWN_TIMEOUT_SECONDS = 10.0

lock_file_handle = None
child_proc = None
running = True
shutdown_event = Event()
wake_lock_acquired = False
pid_file_owned = False
cleanup_complete = False


def log(message: str) -> None:
    """Print a supervisor message without exposing configuration values."""
    print(f"[Supervisor] {message}", flush=True)


def load_env_value(key: str, env_path: Path) -> str:
    """Read one simple KEY=value entry without modifying the process environment."""
    try:
        with env_path.open("r", encoding="utf-8") as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, value = line.split("=", 1)
                if name.strip() == key:
                    return value.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def is_real_token(value: str) -> bool:
    """Reject missing and documented placeholder values without logging tokens."""
    return bool(value) and not value.startswith("replace-with-") and value != "your-token-from-botfather"


def validate_configuration() -> str | None:
    """Return a safe configuration error message, if startup cannot succeed."""
    env_token = os.environ.get("BOT_TOKEN", "").strip()
    if "BOT_TOKEN" in os.environ:
        token = env_token
    else:
        token = load_env_value("BOT_TOKEN", SCRIPT_DIR / ".env")
    if not is_real_token(token):
        return "BOT_TOKEN is not set. Add it to .env or the environment."
    return None


def ensure_runtime_dir() -> None:
    """Create the supervisor's private runtime directory when needed."""
    RUNTIME_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(RUNTIME_DIR, 0o700)
    except OSError:
        # chmod is not available or meaningful on every supported platform.
        pass


def write_pid_file() -> None:
    """Atomically publish this supervisor's PID after its lock is held."""
    global pid_file_owned
    payload = {"pid": os.getpid(), "started_at": int(time.time())}
    fd, temp_name = tempfile.mkstemp(prefix=f".{PID_FILE.name}.", dir=RUNTIME_DIR, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as pid_file:
            json.dump(payload, pid_file)
            pid_file.write("\n")
            pid_file.flush()
            os.fsync(pid_file.fileno())
        os.replace(temp_name, PID_FILE)
        pid_file_owned = True
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def acquire_single_instance_lock() -> bool:
    """Acquire an OS-backed lock; a leftover PID file is never treated as a lock."""
    global lock_file_handle
    ensure_runtime_dir()
    try:
        lock_file_handle = LOCK_FILE.open("a+", encoding="utf-8")
        # Windows locks a byte range, so ensure byte zero exists before locking it.
        lock_file_handle.seek(0)
        if not lock_file_handle.read(1):
            lock_file_handle.seek(0)
            lock_file_handle.write("0")
            lock_file_handle.flush()

        if os.name == "nt":
            import msvcrt

            lock_file_handle.seek(0)
            msvcrt.locking(lock_file_handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        write_pid_file()
        return True
    except (OSError, IOError) as error:
        log(f"another supervisor instance is already running ({error}).")
        if lock_file_handle is not None:
            try:
                lock_file_handle.close()
            except OSError:
                pass
            lock_file_handle = None
        return False


def release_single_instance_lock() -> None:
    """Release this process's lock and remove only the PID file it created."""
    global lock_file_handle, pid_file_owned
    if pid_file_owned:
        try:
            payload = json.loads(PID_FILE.read_text(encoding="utf-8"))
            if payload.get("pid") == os.getpid():
                PID_FILE.unlink()
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        pid_file_owned = False

    if lock_file_handle is not None:
        try:
            if os.name == "nt":
                import msvcrt

                lock_file_handle.seek(0)
                msvcrt.locking(lock_file_handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file_handle.fileno(), fcntl.LOCK_UN)
        except (OSError, IOError):
            pass
        finally:
            try:
                lock_file_handle.close()
            except OSError:
                pass
            lock_file_handle = None


def acquire_wake_lock() -> None:
    """Acquire a Termux wake lock only when this supervisor owns it."""
    global wake_lock_acquired
    executable = shutil.which("termux-wake-lock")
    if not executable:
        return
    try:
        result = subprocess.run([executable], check=False)
        wake_lock_acquired = result.returncode == 0
        if wake_lock_acquired:
            log("Termux wake lock acquired.")
        else:
            log("Termux wake lock command failed; continuing without it.")
    except OSError as error:
        log(f"could not acquire Termux wake lock: {error}")


def release_wake_lock() -> None:
    """Release a wake lock only if this supervisor acquired it."""
    global wake_lock_acquired
    if not wake_lock_acquired:
        return
    executable = shutil.which("termux-wake-unlock")
    if executable:
        try:
            subprocess.run([executable], check=False)
            log("Termux wake lock released.")
        except OSError:
            pass
    wake_lock_acquired = False


def stop_child(sig: int | None = None) -> None:
    """Forward shutdown to the child, then force it down after a bounded wait."""
    global child_proc
    process = child_proc
    if process is None or process.poll() is not None:
        return

    try:
        if os.name != "nt" and hasattr(os, "killpg"):
            os.killpg(process.pid, sig or signal.SIGTERM)
        elif os.name == "nt" and sig is not None and hasattr(signal, "CTRL_BREAK_EVENT"):
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.terminate()
        process.wait(timeout=CHILD_SHUTDOWN_TIMEOUT_SECONDS)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass

    try:
        if os.name != "nt" and hasattr(os, "killpg"):
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=CHILD_SHUTDOWN_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        pass


def handle_termination(sig: int, _frame: object) -> None:
    """Stop accepting restarts and terminate the child on SIGINT/SIGTERM."""
    global running
    if not running:
        return
    log(f"received signal {sig}; shutting down.")
    running = False
    shutdown_event.set()
    stop_child(sig)


def restart_delay(backoff: float) -> float:
    """Return bounded exponential backoff with jitter to avoid restart storms."""
    return min(MAX_BACKOFF_SECONDS, backoff * random.uniform(JITTER_MIN, JITTER_MAX))


def start_child(main_script: Path) -> subprocess.Popen:
    """Start the bot in a separate POSIX process group for signal forwarding."""
    kwargs: dict[str, object] = {"cwd": SCRIPT_DIR}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen([sys.executable, str(main_script)], **kwargs)


def cleanup() -> None:
    """Release resources exactly once, regardless of the termination path."""
    global cleanup_complete
    if cleanup_complete:
        return
    cleanup_complete = True
    stop_child()
    release_wake_lock()
    release_single_instance_lock()


def run_supervisor() -> int:
    """Run main.py and restart only transient failures."""
    global child_proc, running
    config_error = validate_configuration()
    if config_error:
        log(f"configuration error: {config_error}")
        return EXIT_CONFIG

    if not acquire_single_instance_lock():
        return EXIT_OK

    atexit.register(cleanup)
    signal.signal(signal.SIGINT, handle_termination)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_termination)
    acquire_wake_lock()

    main_script = SCRIPT_DIR / "main.py"
    if not main_script.is_file():
        log("configuration error: main.py was not found.")
        return EXIT_CONFIG

    backoff = INITIAL_BACKOFF_SECONDS
    log("reliable supervisor started.")
    try:
        while running:
            started_at = time.monotonic()
            try:
                child_proc = start_child(main_script)
                exit_code = child_proc.wait()
            except OSError as error:
                log(f"could not start bot process: {error}")
                exit_code = 1
            finally:
                child_proc = None

            runtime = time.monotonic() - started_at
            if not running:
                break
            if exit_code == EXIT_OK:
                log("bot exited normally.")
                return EXIT_OK
            if exit_code == EXIT_CONFIG:
                log("bot reported a permanent configuration error; not restarting.")
                return EXIT_CONFIG

            if runtime >= STABLE_RUN_SECONDS:
                backoff = INITIAL_BACKOFF_SECONDS
            delay = restart_delay(backoff)
            log(
                f"bot exited with code {exit_code} after {runtime:.1f}s; "
                f"restarting in {delay:.1f}s."
            )
            backoff = min(MAX_BACKOFF_SECONDS, backoff * 2)
            shutdown_event.wait(delay)
        return EXIT_OK
    finally:
        cleanup()


if __name__ == "__main__":
    sys.exit(run_supervisor())
