"""Tests for the Ubuntu host files in deploy/ubuntu/ and docs/runbooks/ubuntu-host.md
(run: python -m unittest discover -s scripts/tests).

Stdlib only; never imports `shifde`. The runbook's commands are checked against the real CLI,
read with `ast` from backend/src/shifde/cli/main.py and backend/src/shifde/executor/main.py and
from `[project.scripts]` in backend/pyproject.toml."""

from __future__ import annotations

import ast
import re
import shlex
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "deploy" / "ubuntu"
SYSTEMD = DEPLOY / "systemd"
ENV = DEPLOY / "env"
NGINX = DEPLOY / "nginx" / "seth.conf"
RUNBOOK = REPO / "docs" / "runbooks" / "ubuntu-host.md"
CLI_MAIN = REPO / "backend" / "src" / "shifde" / "cli" / "main.py"
EXECUTOR_MAIN = REPO / "backend" / "src" / "shifde" / "executor" / "main.py"
PYPROJECT = REPO / "backend" / "pyproject.toml"

# seth-ops runs only the backup and `seth-admin`; each executor instance has a user of its own
SERVICE_USERS = {"seth", "seth-web", "seth-exec-%i"}
# unit -> minimum TimeoutStopSec: the process's default grace + 10 s
SHIFDE_UNITS = {
    "seth-api.service": 40,
    "seth-worker.service": 40,
    "seth-executor@.service": 70,
}
LONG_RUNNING = (*SHIFDE_UNITS, "seth-web.service")
SANDBOX = {
    "NoNewPrivileges": "yes",
    "ProtectSystem": "strict",
    "ProtectHome": "yes",
    "PrivateTmp": "yes",
    "PrivateDevices": "yes",
    "ProtectKernelTunables": "yes",
    "ProtectKernelModules": "yes",
    "ProtectControlGroups": "yes",
    "ProtectProc": "invisible",
    "RestrictAddressFamilies": "AF_INET AF_INET6 AF_UNIX",
    "RestrictSUIDSGID": "yes",
    "LockPersonality": "yes",
    "CapabilityBoundingSet": "",
}
ADMIN_COMMANDS = ("shifde-admin", "seth-admin")


# --- the CLI as the code defines it ---------------------------------------------------------


def project_scripts(pyproject: Path = PYPROJECT) -> set[str]:
    with pyproject.open("rb") as f:
        return set(tomllib.load(f)["project"]["scripts"])


def _typer_option(node: ast.expr | None) -> ast.Call | None:
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "typer"
        and node.func.attr == "Option"
    ):
        return node
    return None


def _long_options(arg: ast.arg, default: ast.expr | None) -> set[str]:
    """The long options of one parameter: none for a `typer.Argument`."""
    option = _typer_option(default)
    is_bool = False
    annotation = arg.annotation
    if (
        isinstance(annotation, ast.Subscript)
        and isinstance(annotation.value, ast.Name)
        and annotation.value.id == "Annotated"
        and isinstance(annotation.slice, ast.Tuple)
    ):
        first, *metadata = annotation.slice.elts
        is_bool = isinstance(first, ast.Name) and first.id == "bool"
        for item in metadata:
            option = _typer_option(item) or option
    elif isinstance(annotation, ast.Name):
        is_bool = annotation.id == "bool"
    if option is None:
        return set()
    literals = [
        a.value
        for a in option.args
        if isinstance(a, ast.Constant) and isinstance(a.value, str) and a.value.startswith("--")
    ]
    if literals:
        return {name for literal in literals for name in literal.split("/") if name}
    name = "--" + arg.arg.replace("_", "-")
    return {name, "--no-" + name[2:]} if is_bool else {name}


def admin_commands(source: Path = CLI_MAIN) -> dict[tuple[str, ...], set[str]]:
    """`shifde-admin` command paths (`("db", "upgrade")`, `("version",)`) -> long options."""
    tree = ast.parse(source.read_text(encoding="utf-8"))
    typers: set[str] = set()
    groups: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "Typer"
        ):
            typers.update(t.id for t in node.targets if isinstance(t, ast.Name))
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_typer"
            and node.args
            and isinstance(node.args[0], ast.Name)
        ):
            for kw in node.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    groups[node.args[0].id] = kw.value.value
    commands: dict[tuple[str, ...], set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for deco in node.decorator_list:
            if not (
                isinstance(deco, ast.Call)
                and isinstance(deco.func, ast.Attribute)
                and deco.func.attr == "command"
                and isinstance(deco.func.value, ast.Name)
                and deco.func.value.id in typers
            ):
                continue
            owner = deco.func.value.id
            name = (
                deco.args[0].value
                if deco.args and isinstance(deco.args[0], ast.Constant)
                else node.name.replace("_", "-")
            )
            path = (groups[owner], name) if owner in groups else (name,)
            args = node.args
            positional = args.posonlyargs + args.args
            defaults: list[ast.expr | None] = [None] * (len(positional) - len(args.defaults))
            defaults += args.defaults
            pairs = list(zip(positional, defaults, strict=True))
            pairs += list(zip(args.kwonlyargs, args.kw_defaults, strict=True))
            options = {"--help"}
            for arg, default in pairs:
                options |= _long_options(arg, default)
            commands[path] = options
    return commands


def executor_options(source: Path = EXECUTOR_MAIN) -> set[str]:
    tree = ast.parse(source.read_text(encoding="utf-8"))
    options = {"--help"}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
        ):
            options.update(
                a.value
                for a in node.args
                if isinstance(a, ast.Constant)
                and isinstance(a.value, str)
                and a.value.startswith("--")
            )
    return options


# --- the runbook checker --------------------------------------------------------------------


def fenced_lines(markdown: str) -> list[str]:
    """The lines of the fenced code blocks, with backslash continuations joined."""
    lines: list[str] = []
    inside = False
    pending = ""
    for raw in markdown.splitlines():
        if raw.lstrip().startswith("```"):
            inside = not inside
            pending = ""
            continue
        if not inside:
            continue
        if raw.rstrip().endswith("\\"):
            pending += raw.rstrip()[:-1] + " "
            continue
        lines.append(pending + raw)
        pending = ""
    return lines


def _words(segment: str) -> list[str]:
    try:
        return shlex.split(segment, comments=True)
    except ValueError:
        return segment.split()


def check_runbook(
    markdown: str,
    commands: dict[tuple[str, ...], set[str]],
    scripts: set[str],
    executor: set[str],
) -> list[str]:
    """One problem per unknown command, subcommand or long option in the fenced blocks."""
    problems: list[str] = []
    groups = {path[0] for path in commands if len(path) == 2}
    for line in fenced_lines(markdown):
        for token in re.findall(r"\bshifde-[a-z][a-z-]*", line):
            if token not in scripts:
                problems.append(f"unknown command {token!r}: {line.strip()}")
        for segment in re.split(r"\|\||&&|[|;()]", line):
            words = _program_and_arguments(_words(segment))
            if not words:
                continue
            program = words[0].rsplit("/", 1)[-1]
            if program in ADMIN_COMMANDS:
                problems += _check_admin(words[1:], commands, groups, line)
            elif program == "shifde-executor":
                problems += [
                    f"unknown option {opt!r} of shifde-executor: {line.strip()}"
                    for opt in _long(words[1:])
                    if opt not in executor
                ]
    return problems


def _program_and_arguments(words: list[str]) -> list[str]:
    """The command a shell segment runs: without a leading `sudo` (and its options), `env` and
    `VAR=value` words."""
    while words and (
        words[0] in ("sudo", "env", "exec")
        or (words[0].startswith("-") and words[0] != "--")
        or re.fullmatch(r"[A-Za-z_]\w*=.*", words[0])
    ):
        if words[0] == "-u":
            words = words[1:]  # sudo -u USER
        words = words[1:]
    return words


def _long(words: list[str]) -> list[str]:
    return [w.split("=", 1)[0] for w in words if w.startswith("--")]


def _check_admin(
    words: list[str], commands: dict[tuple[str, ...], set[str]], groups: set[str], line: str
) -> list[str]:
    plain = [w for w in words if not w.startswith("-")]
    if not plain:
        bad = [o for o in _long(words) if o != "--help"]
        return [f"unknown option {o!r} of shifde-admin: {line.strip()}" for o in bad]
    if plain[0] in groups:
        path: tuple[str, ...] = tuple(plain[:2])
    else:
        path = (plain[0],)
    if path not in commands:
        return [f"unknown command 'shifde-admin {' '.join(path)}': {line.strip()}"]
    return [
        f"unknown option {o!r} of 'shifde-admin {' '.join(path)}': {line.strip()}"
        for o in _long(words)
        if o not in commands[path]
    ]


# --- unit, env and nginx parsing ------------------------------------------------------------


def read_unit(path: Path) -> dict[str, dict[str, list[str]]]:
    sections: dict[str, dict[str, list[str]]] = {}
    current: dict[str, list[str]] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
            continue
        key, _, value = line.partition("=")
        current.setdefault(key.strip(), []).append(value.strip())
    return sections


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def env_example_for(environment_file: str) -> Path:
    """The example in deploy/ubuntu/env/ of an installed /etc/seth/ env file."""
    if environment_file.startswith("/etc/seth/executor/"):
        return ENV / "executor.env.example"
    return ENV / (environment_file.rsplit("/", 1)[-1] + ".example")


def seconds(value: str) -> int:
    match = re.fullmatch(r"(\d+)\s*(s|min)?", value)
    if not match:
        raise ValueError(f"unsupported time span {value!r}")
    return int(match.group(1)) * (60 if match.group(2) == "min" else 1)


class RunbookCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.commands = admin_commands()
        self.scripts = project_scripts()
        self.executor = executor_options()

    def check(self, markdown: str) -> list[str]:
        return check_runbook(markdown, self.commands, self.scripts, self.executor)

    def test_the_cli_tree_is_read_from_the_code(self) -> None:
        self.assertEqual(
            self.scripts, {"shifde-api", "shifde-worker", "shifde-admin", "shifde-executor"}
        )
        self.assertEqual(self.commands[("backup", "verify")], {"--from", "--help"})
        self.assertIn("--env-file", self.commands[("config", "preflight")])
        self.assertIn(("db", "upgrade"), self.commands)
        self.assertIn(("version",), self.commands)
        self.assertIn("--no-reuse-passwords", self.commands[("dev-seed",)])
        self.assertEqual(self.executor, {"--fake-platform", "--help"})

    def test_the_runbook_uses_only_real_commands_and_options(self) -> None:
        markdown = RUNBOOK.read_text(encoding="utf-8")
        self.assertIn("sudo seth-admin backup verify --from", markdown)
        self.assertEqual(self.check(markdown), [])

    def test_an_unknown_subcommand_and_an_unknown_option_are_reported(self) -> None:
        sample = "\n".join(
            [
                "Prose mentions `shifde-admin backup restore`, which is not checked.",
                "```bash",
                "sudo seth-admin db upgrade",
                "sudo seth-admin backup restore --from /var/backups/seth/x",
                "sudo seth-admin backup verify \\",
                "  --frm /var/backups/seth/x",
                "/opt/seth/backend/.venv/bin/shifde-admin version --json",
                "shifde-executor --fake-platform",
                "shifde-agent --help",
                "```",
            ]
        )
        problems = self.check(sample)
        self.assertEqual(len(problems), 3, problems)
        self.assertIn("unknown command 'shifde-admin backup restore'", problems[0])
        self.assertIn("unknown option '--frm' of 'shifde-admin backup verify'", problems[1])
        self.assertIn("unknown command 'shifde-agent'", problems[2])

    def test_the_real_runbook_with_a_wrong_option_fails(self) -> None:
        markdown = RUNBOOK.read_text(encoding="utf-8").replace(
            "backup verify --from", "backup verify --frm"
        )
        problems = self.check(markdown)
        self.assertTrue(problems)
        self.assertTrue(all("'--frm'" in p for p in problems), problems)


class UnitFileTests(unittest.TestCase):
    def unit(self, name: str) -> dict[str, dict[str, list[str]]]:
        return read_unit(SYSTEMD / name)

    def test_every_long_running_unit_has_the_common_settings(self) -> None:
        for name in LONG_RUNNING:
            with self.subTest(unit=name):
                unit = self.unit(name)
                service = unit["Service"]
                self.assertIn(service["User"][0], SERVICE_USERS)
                self.assertEqual(service["Group"], service["User"])
                for env_file in service["EnvironmentFile"]:
                    self.assertTrue(env_file.startswith("/etc/seth/"), env_file)
                (exec_start,) = service["ExecStart"]
                self.assertNotIn("uv run", exec_start)
                program = exec_start.split()[0]
                if name == "seth-web.service":
                    self.assertEqual(program, "/usr/bin/node")
                    self.assertIn("/opt/seth/frontend/node_modules/next/", exec_start)
                    self.assertNotIn("MemoryDenyWriteExecute", service)
                else:
                    self.assertTrue(program.startswith("/opt/seth/backend/.venv/bin/shifde-"))
                    self.assertIn(program.rsplit("/", 1)[-1], project_scripts())
                self.assertTrue(service["WorkingDirectory"][0].startswith("/opt/seth/"))
                self.assertIn("network-online.target", unit["Unit"]["After"][0].split())
                self.assertIn("network-online.target", unit["Unit"]["Wants"][0].split())
                self.assertEqual(unit["Unit"]["StartLimitIntervalSec"], ["0"])
                self.assertEqual(service["StandardOutput"], ["journal"])
                self.assertEqual(service["StandardError"], ["journal"])
                self.assertTrue(service["SyslogIdentifier"][0])
                self.assertEqual(service["UMask"], ["0077"])
                for key, value in SANDBOX.items():
                    self.assertEqual(service.get(key), [value], key)
                self.assertEqual(service["Restart"], ["on-failure"])
                self.assertEqual(service["RestartSec"], ["1s"])
                self.assertTrue(service["RestartSteps"][0].isdigit())
                self.assertEqual(service["RestartMaxDelaySec"], ["60s"])
                self.assertEqual(service["KillSignal"], ["SIGTERM"])

    def test_the_shifde_units_never_restart_on_exit_2_to_5(self) -> None:
        for name, minimum in SHIFDE_UNITS.items():
            with self.subTest(unit=name):
                service = self.unit(name)["Service"]
                (statuses,) = service["RestartPreventExitStatus"]
                self.assertEqual(sorted(statuses.split()), ["2", "3", "4", "5"])
                self.assertGreaterEqual(seconds(service["TimeoutStopSec"][0]), minimum)
        self.assertNotIn("RestartPreventExitStatus", self.unit("seth-web.service")["Service"])

    def test_each_health_check_uses_a_port_set_in_its_env_example(self) -> None:
        for name in LONG_RUNNING:
            with self.subTest(unit=name):
                service = self.unit(name)["Service"]
                (post,) = service["ExecStartPost"]
                self.assertTrue(post.startswith("/usr/bin/curl --fail "), post)
                self.assertIn("--retry-connrefused", post)
                match = re.search(r"http://127\.0\.0\.1:\$\{(\w+)\}/(\S*)$", post)
                assert match, post
                variable, path = match.groups()
                self.assertEqual(path, "" if name == "seth-web.service" else "healthz")
                retry_max = int(re.search(r"--retry-max-time (\d+)", post).group(1))  # type: ignore[union-attr]
                self.assertLess(retry_max + 2, seconds(service["TimeoutStartSec"][0]))
                examples = [read_env(env_example_for(f)) for f in service["EnvironmentFile"]]
                self.assertTrue(any(variable in e for e in examples), variable)
        self.assertEqual(read_env(ENV / "seth.env.example")["SHIFDE_API_PORT"], "8710")
        self.assertEqual(read_env(ENV / "seth.env.example")["SHIFDE_WORKER_HEALTH_PORT"], "8711")
        self.assertEqual(read_env(ENV / "web.env.example")["SHIFDE_WEB_PORT"], "3710")
        self.assertEqual(
            read_env(ENV / "executor.env.example")["SHIFDE_EXECUTOR_HEALTH_PORT"], "8720"
        )

    def test_the_per_unit_specifics(self) -> None:
        self.assertEqual(
            self.unit("seth-executor@.service")["Service"]["EnvironmentFile"],
            ["/etc/seth/executor/%i.env"],
        )
        self.assertNotIn(
            "--fake-platform", self.unit("seth-executor@.service")["Service"]["ExecStart"][0]
        )
        users = {
            name: self.unit(name)["Service"]["User"][0]
            for name in (*LONG_RUNNING, "seth-backup.service")
        }
        # the owner credential's user and each organization's executor share no uid
        self.assertEqual(users["seth-executor@.service"], "seth-exec-%i")
        self.assertEqual(list(users.values()).count("seth-ops"), 1)
        self.assertNotIn("seth-ops", {users[n] for n in LONG_RUNNING})
        web = self.unit("seth-web.service")["Service"]
        self.assertEqual(web["EnvironmentFile"], ["/etc/seth/web.env"])
        self.assertTrue(web["ExecStart"][0].endswith("start -p ${SHIFDE_WEB_PORT} -H 127.0.0.1"))
        for name in ("seth-api.service", "seth-worker.service"):
            self.assertEqual(self.unit(name)["Service"]["EnvironmentFile"], ["/etc/seth/seth.env"])
        backup = self.unit("seth-backup.service")["Service"]
        self.assertEqual(backup["Type"], ["oneshot"])
        self.assertEqual(backup["User"], ["seth-ops"])
        for key, value in SANDBOX.items():
            self.assertEqual(backup.get(key), [value], key)
        self.assertEqual(backup["EnvironmentFile"], ["/etc/seth/seth.env", "/etc/seth/admin.env"])
        self.assertEqual(backup["ReadWritePaths"], ["/var/backups/seth"])
        (exec_start,) = backup["ExecStart"]
        self.assertIn("shifde-admin backup create --out", exec_start)
        self.assertIn("shifde-admin backup verify --from", exec_start)
        timer = self.unit("seth-backup.timer")["Timer"]
        self.assertEqual(timer["OnCalendar"], ["daily"])
        self.assertEqual(timer["Persistent"], ["true"])

    def test_env_files_live_under_etc_seth(self) -> None:
        for path in sorted(SYSTEMD.iterdir()):
            for values in read_unit(path).values():
                for env_file in values.get("EnvironmentFile", []):
                    self.assertTrue(env_file.startswith("/etc/seth/"), f"{path.name}: {env_file}")


class EnvAndEdgeTests(unittest.TestCase):
    def test_env_examples(self) -> None:
        seth = read_env(ENV / "seth.env.example")
        self.assertEqual(seth["SHIFDE_ENV"], "production")
        self.assertEqual(seth["SHIFDE_API_HOST"], "127.0.0.1")
        self.assertEqual(seth["SHIFDE_TRUSTED_PROXIES"], "127.0.0.1/32")
        self.assertEqual(seth["SHIFDE_TRUST_LOOPBACK_PROXY"], "false")
        self.assertEqual(seth["SHIFDE_API_DOCS_ENABLED"], "false")
        self.assertEqual(seth["SHIFDE_AUTH_LOCAL_ENABLED"], "false")
        self.assertEqual(seth["SHIFDE_HSTS_MAX_AGE_SECONDS"], "31536000")
        self.assertIn("sslmode=verify-full", seth["SHIFDE_DATABASE_URL"])
        self.assertNotIn("SHIFDE_MIGRATION_DATABASE_URL", seth)
        admin = read_env(ENV / "admin.env.example")
        self.assertEqual(set(admin), {"SHIFDE_MIGRATION_DATABASE_URL", "SHIFDE_PG_BIN_DIR"})
        self.assertEqual(admin["SHIFDE_PG_BIN_DIR"], "/usr/lib/postgresql/17/bin")
        web = read_env(ENV / "web.env.example")
        self.assertEqual(web["SHIFDE_WEB_API_PROXY"], "off")
        self.assertEqual(web["NODE_ENV"], "production")
        executor = read_env(ENV / "executor.env.example")
        self.assertTrue(executor["SHIFDE_EXECUTOR_API_URL"].startswith("https://"))

    def test_nginx_routes_to_the_example_ports_and_overwrites_x_forwarded_for(self) -> None:
        conf = NGINX.read_text(encoding="utf-8")
        api_port = read_env(ENV / "seth.env.example")["SHIFDE_API_PORT"]
        web_port = read_env(ENV / "web.env.example")["SHIFDE_WEB_PORT"]
        upstreams = re.findall(r"^\s*proxy_pass (\S+);", conf, re.M)
        self.assertEqual(
            upstreams,
            [
                f"http://127.0.0.1:{api_port}",
                f"http://127.0.0.1:{api_port}",
                f"http://127.0.0.1:{web_port}",
            ],
        )
        self.assertEqual(conf.count("proxy_set_header X-Forwarded-For $remote_addr;"), 3)
        self.assertNotIn("proxy_add_x_forwarded_for", conf)
        directives = "\n".join(
            line for line in conf.splitlines() if not line.lstrip().startswith("#")
        )
        self.assertNotIn("localhost", directives)
        self.assertIsNone(re.search(r"^\s*add_header", conf, re.M))
        self.assertIn("location ~ ^/api/metrics(/|$) {\n        return 404;", conf)
        self.assertIn("return 301 https://$host$request_uri;", conf)
        self.assertIn("ssl_protocols TLSv1.2 TLSv1.3;", conf)
        self.assertIn("client_max_body_size 8m;", conf)
        timeout = re.search(r"proxy_read_timeout (\d+)s;", conf)
        assert timeout
        self.assertGreaterEqual(int(timeout.group(1)), 180)

    def test_the_admin_wrapper_runs_as_seth_with_both_env_files(self) -> None:
        wrapper = (DEPLOY / "seth-admin").read_text(encoding="utf-8")
        self.assertTrue(wrapper.startswith("#!/usr/bin/env bash\n"))
        self.assertIn(
            "systemd-run --quiet --pipe --wait --collect --uid=seth-ops --gid=seth-ops", wrapper
        )
        self.assertIn("-p EnvironmentFile=/etc/seth/seth.env", wrapper)
        self.assertIn("-p EnvironmentFile=/etc/seth/admin.env", wrapper)
        self.assertIn('/opt/seth/backend/.venv/bin/shifde-admin "$@"', wrapper)


@unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze is not installed")
class SystemdAnalyzeTests(unittest.TestCase):
    def test_systemd_analyze_verify_accepts_the_units(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stub = root / "stub"
            stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            stub.chmod(0o755)
            copies: list[Path] = []
            for path in sorted(SYSTEMD.iterdir()):
                # the template is checked as an instance, with %i resolved
                name = path.name.replace("@.", "@example.")
                text = re.sub(
                    r"^(Exec\w+=[-@:+!]*)/\S+",
                    lambda m: m.group(1) + str(stub),
                    path.read_text(encoding="utf-8"),
                    flags=re.M,
                )
                (root / name).write_text(text, encoding="utf-8")
                copies.append(root / name)
            result = subprocess.run(
                ["systemd-analyze", "verify", "--man=no", *map(str, copies)],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            named = [line for line in output.splitlines() if any(c.name in line for c in copies)]
            self.assertEqual(named, [], output)


if __name__ == "__main__":
    unittest.main()
