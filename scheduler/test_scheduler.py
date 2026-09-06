import importlib.util
import json
import os
import pathlib
import signal
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock


MODULE_PATH = pathlib.Path(__file__).with_name("scheduler.py")
SPEC = importlib.util.spec_from_file_location("scheduler", MODULE_PATH)
scheduler = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scheduler)


class SchedulerTest(unittest.TestCase):
    @mock.patch.object(scheduler, "github_pages")
    def test_stuck_dynamic_run_requires_missing_online_label(self, pages):
        created_at = datetime.fromtimestamp(100, timezone.utc).isoformat()
        pages.side_effect = [
            [{"status": "online", "labels": [{"name": "worker-other"}]}],
            [{"full_name": "corioders/app"}],
            [{"id": 10, "run_attempt": 1, "created_at": created_at, "jobs_url": "jobs"}],
            [{"status": "queued", "labels": ["self-hosted", "corioders-worker-vu-compute-3"]}],
        ]

        self.assertEqual(
            [("corioders/app", 10)],
            [(repository, run["id"]) for repository, run in scheduler.stuck_dynamic_runs("token", "corioders", 600, now=1000)],
        )

    @mock.patch.object(scheduler, "github_pages")
    def test_online_dynamic_label_is_not_rescued(self, pages):
        created_at = datetime.fromtimestamp(100, timezone.utc).isoformat()
        pages.side_effect = [
            [{"status": "online", "labels": [{"name": "corioders-worker-vu-compute-3"}]}],
            [{"full_name": "corioders/app"}],
            [{"id": 10, "run_attempt": 1, "created_at": created_at, "jobs_url": "jobs"}],
            [{"status": "queued", "labels": ["corioders-worker-vu-compute-3"]}],
        ]

        self.assertEqual([], list(scheduler.stuck_dynamic_runs("token", "corioders", 600, now=1000)))

    def test_stop_workers_terminates_process_groups(self):
        running = mock.Mock()
        running.pid = 123
        running.poll.side_effect = [None, None, 0]
        finished = mock.Mock()
        finished.poll.return_value = 0

        with mock.patch.object(scheduler.os, "killpg") as killpg:
            scheduler.stop_workers([running, finished], timeout=1)

        killpg.assert_called_once_with(123, signal.SIGTERM)
        running.wait.assert_called_once_with()
        finished.wait.assert_called_once_with()

    def telemetry(self, host, priority, cpu=0, ram=0, gpu=0, gpu_memory=0):
        return {
            "available": True,
            "cpu": cpu,
            "gpu": gpu,
            "gpu_memory": gpu_memory,
            "host": host,
            "priority": priority,
            "ram": ram,
            "reachable": True,
        }

    def test_vu_priority_precedes_less_loaded_fallback(self):
        ranked = scheduler.rank_telemetry(
            [
                self.telemetry("windows-wsl", 2, cpu=0.01),
                self.telemetry("vu-compute-1", 1, cpu=0.8),
            ]
        )
        self.assertEqual("vu-compute-1", ranked[0]["host"])

    def test_central_priority_places_external_between_wsl_and_macbook(self):
        ranked = scheduler.rank_telemetry(
            [
                self.telemetry("macbook-pro-2015", 1000),
                self.telemetry("external-alice", 3),
                self.telemetry("windows-wsl", 2),
                self.telemetry("vu-compute-1", 1),
            ]
        )
        self.assertEqual(
            [
                "vu-compute-1",
                "windows-wsl",
                "external-alice",
                "macbook-pro-2015",
            ],
            [item["host"] for item in ranked],
        )

    def test_capacity_can_be_configured_by_target_environment(self):
        with mock.patch.dict(
            os.environ,
            {"CORIODERS_RUNNER_CAPACITY": "4"},
            clear=False,
        ):
            arguments = scheduler.parser().parse_args(["probe", "corioders/repo"])
        self.assertEqual(4, arguments.capacity)

    def test_environment_capacity_must_be_positive(self):
        with mock.patch.dict(
            os.environ,
            {"CORIODERS_RUNNER_CAPACITY": "0"},
            clear=False,
        ):
            with self.assertRaises(SystemExit):
                scheduler.parser()

    def test_highest_resource_pressure_decides_vu_order(self):
        ranked = scheduler.rank_telemetry(
            [
                self.telemetry("vu-compute-1", 1, cpu=0.1, gpu=0.9),
                self.telemetry("vu-compute-2", 1, cpu=0.3, gpu=0.2),
            ]
        )
        self.assertEqual("vu-compute-2", ranked[0]["host"])

    def test_unavailable_target_is_ignored(self):
        unavailable = self.telemetry("vu-compute-1", 1)
        unavailable["available"] = False
        ranked = scheduler.rank_telemetry(
            [unavailable, self.telemetry("windows-wsl", 2)]
        )
        self.assertEqual(["windows-wsl"], [item["host"] for item in ranked])

    def test_repository_worker_scopes(self):
        self.assertEqual("corioders", scheduler.worker_scope("corioders/repo"))
        self.assertEqual("poland20", scheduler.worker_scope("poland2-0/poland20"))
        self.assertEqual("watjurk-wjsetup", scheduler.worker_scope("watjurk/wjsetup"))

    def test_scheduler_host_has_no_implicit_preference(self):
        ranked = scheduler.rank_telemetry(
            [
                self.telemetry("vu-compute-2", 1, cpu=0.4),
                self.telemetry("vu-compute-1", 1, cpu=0.2),
            ]
        )
        self.assertEqual("vu-compute-1", ranked[0]["host"])

    @mock.patch.object(scheduler.subprocess, "run")
    def test_same_host_still_uses_ssh_handoff(self, run):
        run.return_value = mock.Mock(returncode=0)
        scheduler.run_target_command(
            {"host": "macbook-pro-2015", "ssh": "macbook15"},
            ["probe", "watjurk/wjsetup"],
            5,
        )
        self.assertEqual("ssh", run.call_args.args[0][0])
        self.assertEqual("macbook15", run.call_args.args[0][5])

    def test_lease_is_atomic_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(scheduler.write_lease(directory, "10", "1", "a/b", 900))
            self.assertTrue(scheduler.write_lease(directory, "11", "1", "a/b", 900))
            self.assertTrue(scheduler.release_lease(directory, "10", "1"))
            self.assertTrue(scheduler.write_lease(directory, "11", "1", "a/b", 900))

    def test_pool_accepts_exactly_twenty_leases(self):
        with tempfile.TemporaryDirectory() as directory:
            results = []
            with scheduler.concurrent.futures.ThreadPoolExecutor(
                max_workers=25
            ) as executor:
                futures = [
                    executor.submit(
                        scheduler.write_lease,
                        directory,
                        str(run_id),
                        "1",
                        "corioders/a" if run_id % 2 else "watjurk/wjsetup",
                        900,
                    )
                    for run_id in range(1, 26)
                ]
                results = [future.result() for future in futures]
            self.assertEqual(20, sum(results))
            self.assertEqual(
                20, len(scheduler.active_lease_paths(directory, 900))
            )

    def test_worker_specs_follow_active_lease_scopes(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(
                scheduler.write_lease(directory, "10", "1", "corioders/repo", 900)
            )
            self.assertTrue(
                scheduler.write_lease(
                    directory, "11", "1", "poland2-0/poland20", 900
                )
            )
            specs = scheduler.worker_specs(
                directory,
                {"corioders": "/runner/org", "poland20": "/runner/poland20"},
                900,
            )
            self.assertEqual({"/runner/org", "/runner/poland20"}, set(specs.values()))

    def test_stale_lease_is_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            directory_path = scheduler.leases_directory(directory)
            directory_path.mkdir()
            path = scheduler.lease_path(directory, "10", "1")
            path.write_text(json.dumps({"created_at": 1, "run_id": "10"}))
            self.assertEqual([], scheduler.active_lease_paths(directory, 10, now=20))
            self.assertFalse(path.exists())

    def test_release_lease_path_removes_completed_worker_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(
                scheduler.write_lease(directory, "10", "1", "corioders/repo", 900)
            )
            path = scheduler.lease_path(directory, "10", "1")

            scheduler.release_lease_path(directory, path)

            self.assertFalse(path.exists())

    @mock.patch.object(scheduler.time, "sleep")
    @mock.patch.object(scheduler, "run_target_command")
    @mock.patch.object(scheduler, "probe_target")
    def test_scheduler_retries_temporary_saturation(self, probe, run_target, sleep):
        target = {
            "host": "vu-compute-1",
            "label": "worker-1",
            "priority": 1,
            "ssh": "vu_compute_1",
        }
        unavailable = self.telemetry("vu-compute-1", 1)
        unavailable["available"] = False
        probe.side_effect = [unavailable, self.telemetry("vu-compute-1", 1)]
        run_target.return_value = mock.Mock(returncode=0)
        selected = scheduler.schedule(
            [target], "10", "1", "a/b", 5, wait_timeout=10, retry_interval=1
        )
        self.assertEqual("vu-compute-1", selected["host"])
        sleep.assert_called_once_with(1)

    def test_fork_pull_request_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            event_path = pathlib.Path(directory) / "event.json"
            event_path.write_text(
                json.dumps(
                    {
                        "pull_request": {
                            "head": {"repo": {"fork": True, "full_name": "fork/repo"}},
                            "base": {"repo": {"full_name": "corioders/repo"}},
                        }
                    }
                )
            )
            with self.assertRaises(SystemExit):
                scheduler.validate_event("pull_request", event_path)

    def test_same_repository_pull_request_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            event_path = pathlib.Path(directory) / "event.json"
            event_path.write_text(
                json.dumps(
                    {
                        "pull_request": {
                            "head": {
                                "repo": {"fork": False, "full_name": "corioders/repo"}
                            },
                            "base": {"repo": {"full_name": "corioders/repo"}},
                        }
                    }
                )
            )
            scheduler.validate_event("pull_request", event_path)

    @mock.patch.object(scheduler.sys, "platform", "darwin")
    @mock.patch.object(scheduler.subprocess, "run")
    def test_darwin_memory_pressure(self, run):
        run.side_effect = [
            mock.Mock(stdout="16384\n"),
            mock.Mock(
                stdout=(
                    "Mach Virtual Memory Statistics: (page size of 4096 bytes)\n"
                    "Pages free: 1.\n"
                    "Pages inactive: 1.\n"
                    "Pages speculative: 1.\n"
                )
            ),
        ]
        self.assertEqual(0.25, scheduler.memory_pressure())

    def test_ssh_entrypoint_rejects_scheduler_command(self):
        previous = os.environ.get("SSH_ORIGINAL_COMMAND")
        os.environ["SSH_ORIGINAL_COMMAND"] = (
            '"$HOME/.nix-profile/bin/corioders-runner-target" schedule'
        )
        try:
            with self.assertRaises(SystemExit):
                scheduler.command_serve_ssh(None)
        finally:
            if previous is None:
                os.environ.pop("SSH_ORIGINAL_COMMAND", None)
            else:
                os.environ["SSH_ORIGINAL_COMMAND"] = previous


if __name__ == "__main__":
    unittest.main()
