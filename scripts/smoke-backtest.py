"""Run browser smoke with a private local API/worker and disposable SQLite DB."""
import os
from pathlib import Path
import socket
import signal
import subprocess
import tempfile
import time
from urllib.request import urlopen

root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix="arb-backtest-smoke-") as state:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        web_port = sock.getsockname()[1]
    env = {**os.environ, "ARB_BACKTEST_DB": str(Path(state) / "jobs.sqlite3"),
           "ARB_BACKTEST_API_URL": f"http://127.0.0.1:{port}", "ARB_BACKTEST_LIVE": "1", "SMOKE_PORT": str(web_port)}
    children = []
    with (Path(state) / "services.log").open("w+") as log:
        try:
            for command in [
                ["uv", "run", "--locked", "uvicorn", "arb_research.api:create_app", "--factory", "--host", "127.0.0.1", "--port", str(port)],
                ["uv", "run", "--locked", "python", "-m", "arb_research.worker"],
            ]:
                children.append(subprocess.Popen(command, cwd=root / "research", env=env, stdout=log, stderr=log, start_new_session=True))
            for _ in range(100):
                if any(child.poll() is not None for child in children):
                    raise RuntimeError("Local API/worker exited during startup")
                try:
                    with urlopen(f"http://127.0.0.1:{port}/datasets", timeout=1) as response:
                        if response.status == 200:
                            break
                except OSError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("Local API did not become ready")
            # Default: all smoke tests. Extra args can select a spec/project.
            import sys
            result = subprocess.run(["npm", "run", "smoke:web", "--", *sys.argv[1:]], cwd=root, env=env)
            if result.returncode:
                log.seek(0)
                print(log.read())
            raise SystemExit(result.returncode)
        finally:
            for child in reversed(children):
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    child.wait()
