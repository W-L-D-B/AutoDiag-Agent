import io
import json
import os
import signal

import pytest

from scripts import run_bench


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_first_termination_during_cleanup_is_deferred_until_manifest_written(
    tmp_path, monkeypatch, signum
):
    stages = []

    class Process:
        returncode = None

    process = Process()
    output_handle = io.StringIO()
    manifest = {
        "status": "completed",
        "error": None,
        "processes": {"monitor": {"returncode": None}},
    }

    def stop_children(processes):
        stages.append("cleanup_entered")
        os.kill(os.getpid(), signum)
        stages.append("cleanup_continued")
        processes["monitor"].returncode = 0

    def interrupt(_signum, _frame):
        raise KeyboardInterrupt

    monkeypatch.setattr(run_bench, "stop_children", stop_children)
    old_handler = signal.signal(signum, interrupt)
    old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    try:
        with pytest.raises(KeyboardInterrupt):
            run_bench.finalize_run(
                {"monitor": process}, [output_handle], manifest, tmp_path
            )
        assert stages == ["cleanup_entered", "cleanup_continued"]
        assert output_handle.closed
        evidence = json.loads((tmp_path / "manifest.json").read_text())
        assert evidence["status"] == "interrupted"
        assert evidence["processes"]["monitor"]["returncode"] == 0
        assert signal.pthread_sigmask(signal.SIG_BLOCK, set()) == old_mask
    finally:
        signal.signal(signum, old_handler)
