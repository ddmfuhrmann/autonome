import json
import os
import shutil
import subprocess
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "bin" / "autonome"


def make_layout(root: Path, with_dist=True):
    (root / "bin").mkdir()
    shutil.copy(BIN, root / "bin" / "autonome")
    runner = root / "runner"
    (runner / "src").mkdir(parents=True)
    (runner / "src" / "a.ts").write_text("export {};\n")
    (runner / "package.json").write_text("{}\n")
    (runner / "pnpm-lock.yaml").write_text("\n")
    if with_dist:
        (runner / "dist").mkdir()
        (runner / "dist" / "index.js").write_text("console.log('ran');\n")
    fake = root / "fakebin"
    fake.mkdir()
    pnpm = fake / "pnpm"
    pnpm.write_text(f'#!/bin/sh\necho "$@" >> "{root}/pnpm.log"\n')
    pnpm.chmod(0o755)
    return runner


def run(root: Path):
    env = {**os.environ, "PATH": f"{root / 'fakebin'}:{os.environ['PATH']}"}
    r = subprocess.run(
        [str(root / "bin" / "autonome")], capture_output=True, text=True, env=env
    )
    log = root / "pnpm.log"
    return r, log.read_text() if log.exists() else ""


def age(path: Path, seconds: int):
    st = path.stat()
    os.utime(path, (st.st_atime + seconds, st.st_mtime + seconds))


def test_unchanged_sources_do_not_build(tmp_path):
    runner = make_layout(tmp_path)
    age(runner / "dist" / "index.js", 100)
    r, log = run(tmp_path)
    assert r.stdout.strip() == "ran"
    assert log == ""


def test_newer_source_file_builds(tmp_path):
    runner = make_layout(tmp_path)
    age(runner / "dist" / "index.js", 100)
    age(runner / "src" / "a.ts", 200)
    r, log = run(tmp_path)
    assert r.stdout.strip() == "ran"
    assert "build" in log


def test_missing_dist_builds(tmp_path):
    make_layout(tmp_path, with_dist=False)
    r, log = run(tmp_path)
    assert "build" in log
    assert r.returncode != 0  # the fake build produces no dist/index.js


def test_newer_package_json_builds(tmp_path):
    runner = make_layout(tmp_path)
    age(runner / "dist" / "index.js", 100)
    age(runner / "package.json", 200)
    r, log = run(tmp_path)
    assert r.stdout.strip() == "ran"
    assert "build" in log


def test_build_leaves_dist_without_a_stale_test_file(tmp_path):
    build = json.loads((BIN.parents[1] / "runner" / "package.json").read_text())["scripts"]["build"]
    clean, compile_step = build.split(" && ")
    assert compile_step == "tsc"
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "velho.test.js").write_text("old\n")
    (dist / "index.js").write_text("old\n")
    subprocess.run(clean, shell=True, cwd=tmp_path, check=True)  # the step before tsc rewrites dist
    assert not (dist / "velho.test.js").exists()
    assert not dist.exists()


def test_newer_lockfile_builds(tmp_path):
    runner = make_layout(tmp_path)
    age(runner / "dist" / "index.js", 100)
    age(runner / "pnpm-lock.yaml", 200)
    _, log = run(tmp_path)
    assert "build" in log
