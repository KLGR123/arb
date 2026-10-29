"""Authorized local Codex inference, isolated from repo, tools and task secrets."""

import json
import os
import shutil
import signal
import subprocess
import tempfile
import tomllib
from pathlib import Path

from .context import digest, probability
from .runner import now


def isolated_process(args, *, input, cwd, env, text, capture_output, timeout):
    """Join the entire owned process group, including CLI children on timeout."""
    with subprocess.Popen(args, cwd=cwd, env=env, text=text, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          start_new_session=True) as process:
        try:
            stdout, stderr = process.communicate(input=input, timeout=timeout)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise
        return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


class CodexPredictor:
    is_mock = False
    training_cutoff = "2026-06-01T00:00:00Z"
    model = "gpt-6.1-terra"

    def __init__(self, executable="codex", *, effort="low", timeout=180, execute=isolated_process):
        self.executable = shutil.which(executable) or executable
        self.effort, self.timeout, self.execute = effort, timeout, execute

    def predict(self, prompt, *, sample):
        config_path = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
        if config_path.exists():
            config = tomllib.loads(config_path.read_text())
            if any(config.get(k) for k in ("mcp_servers", "hooks", "profiles")):
                raise ValueError("Isolated inference refuses inherited MCP, hooks or profiles")
        started = now()
        with tempfile.TemporaryDirectory(prefix="arb-model-") as scratch:
            schema = Path(scratch) / "response.schema.json"
            schema.write_text(json.dumps({"type": "object", "properties": {"p": {"type": "number"}},
                                          "required": ["p"], "additionalProperties": False}))
            # Keep CLI's configured provider/auth, but remove workspace credentials,
            # read-only proxy, shell exports and global HTTP proxy variables.
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith(("MULTICA_", "ARB_"))
                   and k.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
            args = [self.executable, "exec", "--model", self.model, "--sandbox", "read-only",
                    "--ephemeral", "--skip-git-repo-check", "--ignore-rules", "--json",
                    "--disable", "shell_tool", "--disable", "view_image",
                    "--disable", "image_generation", "--disable", "multi_agent",
                    "--disable", "skill_search", "--disable", "shell_snapshot",
                    "--disable", "code_mode", "--disable", "code_mode_only",
                    "--enable", "skip_host_skill_discovery",
                    "-c", "mcp_servers={}",
                    "-c", 'developer_instructions="Estimate only from the supplied context. No tools or external information."',
                    "-c", "project_doc_max_bytes=0", "-c", 'web_search="disabled"',
                    "-c", "memories.enabled=false", "-c", "memories.generate_memories=false",
                    "-c", "memories.use_memories=false", "-c", 'shell_environment_policy.inherit="none"',
                    "-c", f'model_reasoning_effort="{self.effort}"',
                    "--output-schema", str(schema), "-"]
            try:
                process = self.execute(args, input=prompt, cwd=scratch, env=env,
                                       text=True, capture_output=True, timeout=self.timeout)
            except (OSError, subprocess.TimeoutExpired):
                raise RuntimeError("Codex inference process failed or timed out") from None
            if process.returncode:
                raise RuntimeError("Codex inference returned a nonzero exit status")
            messages, usage = [], None
            for line in process.stdout.splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    raise ValueError("Codex emitted an invalid event stream") from None
                kind = event.get("type")
                item = event.get("item", {})
                # Fail closed if any tool ran, even if the final answer looks valid.
                if kind in ("item.started", "item.completed") and item.get("type") not in (
                        "agent_message", "reasoning", "error"):
                    raise ValueError("Model attempted a tool call; forecast rejected")
                if kind == "item.completed" and item.get("type") == "agent_message":
                    messages.append(item["text"])
                if kind == "turn.completed":
                    usage = {k: v for k, v in event.get("usage", {}).items()
                             if k.endswith("tokens") and isinstance(v, int)}
                if kind in ("error", "turn.failed"):
                    raise RuntimeError("Codex inference reported a failed turn")
            if not messages or usage is None:
                raise ValueError("Codex returned no completed probability response")
            response = json.loads(messages[-1])
            p = probability(response["p"])
            return {"p": p, "audit": {"provider": "local_codex_cli", "model": self.model,
                                      "reasoning_effort": self.effort, "sample": sample,
                                      "started_at": started, "completed_at": now(),
                                      "prompt_sha256": digest(prompt), "usage": usage,
                                      "tool_calls": 0, "sandbox": "read-only"}}
