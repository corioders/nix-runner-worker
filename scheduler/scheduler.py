#!/usr/bin/env python3

import argparse
import concurrent.futures
import contextlib
import fcntl
import json
import os
import pathlib
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone


DEFAULT_LEASE_TTL_SECONDS = 900
DEFAULT_RETRY_INTERVAL_SECONDS = 5
DEFAULT_TARGET_CAPACITY = 20
DEFAULT_WAIT_TIMEOUT_SECONDS = 240
DEFAULT_STUCK_JOB_AGE_SECONDS = 600
REPOSITORY_SCOPES = {
    "poland2-0/poland20": "poland20",
    "watjurk/wjsetup": "watjurk-wjsetup",
}


def fail(message):
    print(message, file=sys.stderr)
    raise SystemExit(1)


def read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def github_request(token, url, method="GET"):
    request = urllib.request.Request(
        url,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2026-03-10",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        if method == "POST" and error.code in {409, 422}:
            return None
        raise
    return json.loads(body) if body else None


def github_pages(token, url, key=None):
    page = 1
    while True:
        separator = "&" if "?" in url else "?"
        payload = github_request(token, f"{url}{separator}per_page=100&page={page}")
        items = payload[key] if key else payload
        yield from items
        if len(items) < 100:
            return
        page += 1


def parse_github_timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def stuck_dynamic_runs(token, organization, minimum_age, now=None):
    now = time.time() if now is None else now
    runners = github_pages(
        token, f"https://api.github.com/orgs/{organization}/actions/runners", "runners"
    )
    online_labels = {
        label["name"]
        for runner in runners
        if runner["status"] == "online"
        for label in runner["labels"]
    }
    repositories = github_pages(
        token, f"https://api.github.com/orgs/{organization}/repos?type=all"
    )
    for repository in repositories:
        full_name = repository["full_name"]
        runs = github_pages(
            token,
            f"https://api.github.com/repos/{full_name}/actions/runs?status=queued",
            "workflow_runs",
        )
        for run in runs:
            if now - parse_github_timestamp(run["created_at"]) < minimum_age:
                continue
            jobs = github_pages(token, run["jobs_url"] + "?filter=latest", "jobs")
            for job in jobs:
                requested = {
                    label
                    for label in job.get("labels", [])
                    if label.startswith("corioders-worker-")
                }
                if job["status"] == "queued" and requested - online_labels:
                    yield full_name, run
                    break


def rescue_run(token, repository, run, wait_timeout=60):
    run_url = f"https://api.github.com/repos/{repository}/actions/runs/{run['id']}"
    github_request(token, run_url + "/cancel", method="POST")
    deadline = time.monotonic() + wait_timeout
    while time.monotonic() < deadline:
        current = github_request(token, run_url)
        if current["run_attempt"] != run["run_attempt"]:
            return False
        if current["status"] == "completed":
            github_request(token, run_url + "/rerun", method="POST")
            return True
        time.sleep(2)
    return False


def validate_event(event_name, event_path):
    if event_name not in {"pull_request", "pull_request_target"}:
        return
    event = read_json(event_path)
    pull_request = event.get("pull_request", {})
    head_repository = pull_request.get("head", {}).get("repo", {})
    base_repository = pull_request.get("base", {}).get("repo", {})
    head_name = head_repository.get("full_name")
    base_name = base_repository.get("full_name")
    if head_repository.get("fork") or not head_name or head_name != base_name:
        fail("fork pull requests may not use corioders self-hosted runners")


def clamp_pressure(value):
    return max(0.0, float(value))


def telemetry_sort_key(telemetry):
    pressures = [
        clamp_pressure(telemetry.get("cpu", 0)),
        clamp_pressure(telemetry.get("ram", 0)),
        clamp_pressure(telemetry.get("gpu", 0)),
        clamp_pressure(telemetry.get("gpu_memory", 0)),
        clamp_pressure(telemetry.get("slots", 0)),
    ]
    return (
        int(telemetry["priority"]),
        max(pressures),
        sum(pressures) / len(pressures),
        telemetry["host"],
    )


def rank_telemetry(items):
    eligible = [
        item
        for item in items
        if item.get("reachable", False) and item.get("available", False)
    ]
    return sorted(eligible, key=telemetry_sort_key)


def memory_pressure():
    if sys.platform == "darwin":
        total_result = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            check=True,
            capture_output=True,
            text=True,
        )
        vm_result = subprocess.run(
            ["vm_stat"], check=True, capture_output=True, text=True
        )
        total = int(total_result.stdout.strip())
        page_size = 4096
        free_pages = 0
        for line in vm_result.stdout.splitlines():
            if "page size of" in line:
                page_size = int(line.split("page size of", 1)[1].split("bytes", 1)[0])
                continue
            name, separator, raw_value = line.partition(":")
            if separator and name in {"Pages free", "Pages inactive", "Pages speculative"}:
                free_pages += int(raw_value.strip().rstrip("."))
        return 1.0 - ((free_pages * page_size) / total) if total else 1.0

    values = {}
    with open("/proc/meminfo", encoding="ascii") as handle:
        for line in handle:
            name, value = line.split(":", 1)
            if name in {"MemTotal", "MemAvailable"}:
                values[name] = int(value.split()[0])
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", 0)
    return 1.0 - (available / total) if total else 1.0


def cpu_pressure():
    load_one, _, _ = os.getloadavg()
    return load_one / max(1, os.cpu_count() or 1)


def gpu_pressures():
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return 0.0, 0.0
    command = [
        nvidia_smi,
        "--query-gpu=utilization.gpu,memory.used,memory.total",
        "--format=csv,noheader,nounits",
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        return 0.0, 0.0
    gpu_utilization = []
    gpu_memory = []
    for line in result.stdout.splitlines():
        try:
            utilization, used, total = [float(part.strip()) for part in line.split(",")]
        except (TypeError, ValueError):
            continue
        gpu_utilization.append(utilization / 100.0)
        gpu_memory.append(used / total if total else 1.0)
    return max(gpu_utilization, default=0.0), max(gpu_memory, default=0.0)


def leases_directory(state_directory):
    return pathlib.Path(state_directory) / "leases"


def lease_path(state_directory, run_id, run_attempt):
    run_id = str(run_id)
    run_attempt = str(run_attempt)
    if not run_id.isdigit() or not run_attempt.isdigit():
        fail("run ID and attempt must be numeric")
    return leases_directory(state_directory) / f"{run_id}-{run_attempt}.json"


def worker_scope(repository):
    if repository.startswith("corioders/"):
        return "corioders"
    return REPOSITORY_SCOPES.get(repository, repository.replace("/", "-"))


def active_lease_paths(state_directory, ttl_seconds, now=None):
    directory = leases_directory(state_directory)
    if not directory.exists():
        return []
    now = time.time() if now is None else now
    active = []
    for path in directory.glob("*.json"):
        try:
            created_at = float(read_json(path)["created_at"])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            created_at = 0
        if now - created_at <= ttl_seconds:
            active.append(path)
            continue
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
    return active


def available_slots(state_directory, repository, capacity, ttl_seconds, now=None):
    active_leases = len(active_lease_paths(state_directory, ttl_seconds, now=now))
    return max(0, capacity - active_leases), capacity


def worker_specs(state_directory, scope_commands, ttl_seconds, now=None):
    specs = {}
    for path in active_lease_paths(state_directory, ttl_seconds, now=now):
        try:
            repository = read_json(path)["repository"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            continue
        command = scope_commands.get(worker_scope(repository))
        if command:
            specs[path] = command
    return specs


@contextlib.contextmanager
def lease_lock(state_directory):
    state_path = pathlib.Path(state_directory)
    state_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = state_path / "leases.lock"
    with open(lock_path, "a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def write_lease(
    state_directory,
    run_id,
    run_attempt,
    repository,
    ttl_seconds,
    capacity=DEFAULT_TARGET_CAPACITY,
):
    with lease_lock(state_directory):
        directory = leases_directory(state_directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = lease_path(state_directory, run_id, run_attempt)
        if path.exists():
            return True
        available, _ = available_slots(
            state_directory, repository, capacity, ttl_seconds
        )
        if available == 0:
            return False
        payload = {
            "created_at": time.time(),
            "repository": repository,
            "run_attempt": str(run_attempt),
            "run_id": str(run_id),
        }
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            return True
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
        return True


def release_lease(state_directory, run_id, run_attempt=None):
    with lease_lock(state_directory):
        directory = leases_directory(state_directory)
        if run_attempt is not None:
            paths = [lease_path(state_directory, run_id, run_attempt)]
        elif str(run_id).isdigit():
            paths = list(directory.glob(f"{run_id}-*.json"))
        else:
            fail("run ID must be numeric")
        for path in paths:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
    return True


def release_lease_path(state_directory, path):
    with lease_lock(state_directory):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def run_target_command(target, arguments, timeout):
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={timeout}",
        target["ssh"],
        '"$HOME/.nix-profile/bin/corioders-runner-target"',
        *arguments,
    ]
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout + 2,
    )


def probe_target(target, timeout):
    try:
        result = run_target_command(target, ["probe", target["repository"]], timeout)
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    try:
        telemetry = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    telemetry.update(
        {
            "host": target["host"],
            "label": target["label"],
            "priority": target["priority"],
            "reachable": True,
        }
    )
    return telemetry


def schedule(
    targets,
    run_id,
    run_attempt,
    repository,
    timeout,
    wait_timeout=DEFAULT_WAIT_TIMEOUT_SECONDS,
    retry_interval=DEFAULT_RETRY_INTERVAL_SECONDS,
):
    targets = [dict(target, repository=repository) for target in targets]
    targets_by_host = {target["host"]: target for target in targets}
    deadline = time.monotonic() + wait_timeout
    while True:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(targets)) as executor:
            telemetry = list(
                executor.map(lambda target: probe_target(target, timeout), targets)
            )
        ranked = rank_telemetry([item for item in telemetry if item is not None])
        for candidate in ranked:
            target = targets_by_host[candidate["host"]]
            result = run_target_command(
                target,
                ["reserve", str(run_id), str(run_attempt), repository],
                timeout,
            )
            if result.returncode == 0:
                return candidate
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            fail(f"no runner target became available within {wait_timeout} seconds")
        time.sleep(min(retry_interval, remaining))


def state_directory_from_environment():
    value = os.environ.get("CORIODERS_RUNNER_STATE_DIR")
    if not value:
        fail("CORIODERS_RUNNER_STATE_DIR is required")
    return value


def environment_integer(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        fail(f"{name} must be an integer")
    if parsed <= 0:
        fail(f"{name} must be positive")
    return parsed


def command_probe(arguments):
    state_directory = state_directory_from_environment()
    gpu, gpu_memory = gpu_pressures()
    slots_available, slots_total = available_slots(
        state_directory,
        arguments.repository,
        arguments.capacity,
        arguments.lease_ttl,
    )
    print(
        json.dumps(
            {
                "available": slots_available > 0,
                "cpu": cpu_pressure(),
                "gpu": gpu,
                "gpu_memory": gpu_memory,
                "ram": memory_pressure(),
                "slots": (
                    1.0 - (slots_available / slots_total) if slots_total else 1.0
                ),
                "slots_available": slots_available,
                "slots_total": slots_total,
            },
            sort_keys=True,
        )
    )


def command_reserve(arguments):
    if not write_lease(
        state_directory_from_environment(),
        arguments.run_id,
        arguments.run_attempt,
        arguments.repository,
        arguments.lease_ttl,
        arguments.capacity,
    ):
        raise SystemExit(75)


def command_release(arguments):
    if not release_lease(
        state_directory_from_environment(), arguments.run_id, arguments.run_attempt
    ):
        raise SystemExit(75)


def stop_workers(processes, timeout=60):
    running = [process for process in processes if process.poll() is None]
    for process in running:
        os.killpg(process.pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while running and time.monotonic() < deadline:
        running = [process for process in running if process.poll() is None]
        if running:
            time.sleep(0.1)
    for process in running:
        os.killpg(process.pid, signal.SIGKILL)
    for process in processes:
        process.wait()


def command_worker_pool(arguments):
    state_directory = state_directory_from_environment()
    for marker in pathlib.Path(state_directory).glob("worker-*.ready"):
        marker.unlink()
    scope_commands = {}
    for value in arguments.scope_command:
        scope, separator, command = value.partition("=")
        if not separator or not scope or not command or scope in scope_commands:
            fail(f"invalid scope command: {value}")
        scope_commands[scope] = command

    children = {}
    stopping = False

    def request_stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    try:
        while not stopping:
            desired = worker_specs(
                state_directory, scope_commands, arguments.lease_ttl
            )
            removed = []
            for path, process in list(children.items()):
                return_code = process.poll()
                if path in desired and return_code is None:
                    continue
                if path in desired and return_code == 0:
                    release_lease_path(state_directory, path)
                    desired.pop(path)
                removed.append(process)
                children.pop(path)
            stop_workers(removed)
            for path, command in desired.items():
                if path in children:
                    continue
                scope = worker_scope(read_json(path)["repository"])
                environment = os.environ.copy()
                environment["CORIODERS_RUNNER_READY_FILE"] = str(
                    pathlib.Path(state_directory)
                    / f"worker-{scope}-{path.stem}.ready"
                )
                environment["CORIODERS_RUNNER_WORK_VOLUME"] = (
                    f"github-runner-{scope}-{path.stem}-work"
                )
                environment["CORIODERS_RUNNER_INSTANCE_STATE_DIRECTORY"] = str(
                    pathlib.Path(state_directory) / "workers" / path.stem
                )
                if arguments.busy_directory:
                    environment["CORIODERS_RUNNER_BUSY_FILE"] = str(
                        pathlib.Path(arguments.busy_directory)
                        / f"github-runner-{path.stem}.heartbeat"
                    )
                children[path] = subprocess.Popen(
                    [command], env=environment, start_new_session=True
                )
            time.sleep(arguments.reconcile_interval)
    finally:
        stop_workers(list(children.values()))


def command_schedule(arguments):
    validate_event(arguments.event_name, arguments.event_path)
    targets = read_json(arguments.targets)
    selected = schedule(
        targets,
        arguments.run_id,
        arguments.run_attempt,
        arguments.repository,
        arguments.connect_timeout,
        arguments.wait_timeout,
        arguments.retry_interval,
    )
    print(json.dumps(selected, sort_keys=True))
    if arguments.github_output:
        with open(arguments.github_output, "a", encoding="utf-8") as handle:
            handle.write(f"runner-label={selected['label']}\n")
            handle.write(f"runner-host={selected['host']}\n")


def command_validate_event(arguments):
    validate_event(arguments.event_name, arguments.event_path)


def command_rescue_stuck_jobs(arguments):
    token = pathlib.Path(arguments.token_file).read_text(encoding="utf-8").strip()
    if not token:
        fail(f"GitHub API token is missing: {arguments.token_file}")
    rescued = 0
    for repository, run in stuck_dynamic_runs(
        token, arguments.organization, arguments.minimum_age
    ):
        if rescue_run(token, repository, run, arguments.wait_timeout):
            rescued += 1
            print(f"rescued {repository} run {run['id']} attempt {run['run_attempt']}")
    return rescued


def command_serve_ssh(_arguments):
    original_command = os.environ.get("SSH_ORIGINAL_COMMAND", "")
    try:
        tokens = shlex.split(original_command)
    except ValueError:
        fail("invalid scheduler SSH command")
    if not tokens or pathlib.PurePath(tokens[0]).name != "corioders-runner-target":
        fail("scheduler SSH key may only invoke corioders-runner-target")
    if len(tokens) < 2 or tokens[1] not in {"probe", "reserve", "release"}:
        fail("scheduler SSH command is not allowed")
    forwarded = parser().parse_args(tokens[1:])
    forwarded.function(forwarded)


def parser():
    root = argparse.ArgumentParser()
    commands = root.add_subparsers(dest="command", required=True)

    probe = commands.add_parser("probe")
    probe.add_argument("repository")
    probe.add_argument(
        "--capacity",
        type=int,
        default=environment_integer(
            "CORIODERS_RUNNER_CAPACITY", DEFAULT_TARGET_CAPACITY
        ),
    )
    probe.add_argument(
        "--lease-ttl",
        type=int,
        default=environment_integer(
            "CORIODERS_RUNNER_LEASE_TTL", DEFAULT_LEASE_TTL_SECONDS
        ),
    )
    probe.set_defaults(function=command_probe)

    reserve = commands.add_parser("reserve")
    reserve.add_argument("run_id")
    reserve.add_argument("run_attempt")
    reserve.add_argument("repository")
    reserve.add_argument(
        "--capacity",
        type=int,
        default=environment_integer(
            "CORIODERS_RUNNER_CAPACITY", DEFAULT_TARGET_CAPACITY
        ),
    )
    reserve.add_argument(
        "--lease-ttl",
        type=int,
        default=environment_integer(
            "CORIODERS_RUNNER_LEASE_TTL", DEFAULT_LEASE_TTL_SECONDS
        ),
    )
    reserve.set_defaults(function=command_reserve)

    release = commands.add_parser("release")
    release.add_argument("run_id")
    release.add_argument("run_attempt", nargs="?")
    release.set_defaults(function=command_release)

    worker_pool = commands.add_parser("worker-pool")
    worker_pool.add_argument("--scope-command", action="append", required=True)
    worker_pool.add_argument("--busy-directory")
    worker_pool.add_argument(
        "--lease-ttl",
        type=int,
        default=environment_integer(
            "CORIODERS_RUNNER_LEASE_TTL", DEFAULT_LEASE_TTL_SECONDS
        ),
    )
    worker_pool.add_argument("--reconcile-interval", type=float, default=1)
    worker_pool.set_defaults(function=command_worker_pool)

    schedule_parser = commands.add_parser("schedule")
    schedule_parser.add_argument("--targets", required=True)
    schedule_parser.add_argument("--run-id", required=True)
    schedule_parser.add_argument("--run-attempt", required=True)
    schedule_parser.add_argument("--repository", required=True)
    schedule_parser.add_argument("--event-name", required=True)
    schedule_parser.add_argument("--event-path", required=True)
    schedule_parser.add_argument("--github-output")
    schedule_parser.add_argument("--connect-timeout", type=int, default=5)
    schedule_parser.add_argument(
        "--retry-interval", type=float, default=DEFAULT_RETRY_INTERVAL_SECONDS
    )
    schedule_parser.add_argument(
        "--wait-timeout", type=float, default=DEFAULT_WAIT_TIMEOUT_SECONDS
    )
    schedule_parser.set_defaults(function=command_schedule)

    validate_event_parser = commands.add_parser("validate-event")
    validate_event_parser.add_argument("--event-name", required=True)
    validate_event_parser.add_argument("--event-path", required=True)
    validate_event_parser.set_defaults(function=command_validate_event)

    rescue = commands.add_parser("rescue-stuck-jobs")
    rescue.add_argument("--organization", required=True)
    rescue.add_argument("--token-file", required=True)
    rescue.add_argument(
        "--minimum-age", type=int, default=DEFAULT_STUCK_JOB_AGE_SECONDS
    )
    rescue.add_argument("--wait-timeout", type=int, default=60)
    rescue.set_defaults(function=command_rescue_stuck_jobs)

    serve_ssh = commands.add_parser("serve-ssh")
    serve_ssh.set_defaults(function=command_serve_ssh)
    return root


def main():
    arguments = parser().parse_args()
    arguments.function(arguments)


if __name__ == "__main__":
    main()
