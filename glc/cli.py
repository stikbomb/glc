import difflib
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, quote

import requests
import typer

app = typer.Typer(help="Manage GitLab CI/CD variables")


def die(msg: str) -> None:
    typer.echo(typer.style("error: ", fg=typer.colors.RED, bold=True) + msg, err=True)
    raise typer.Exit(1)


def ok(msg: str) -> None:
    typer.echo(typer.style("✓ ", fg=typer.colors.GREEN, bold=True) + msg)


def handle_http_error(e: requests.HTTPError) -> None:
    status = e.response.status_code
    messages = {
        401: "unauthorized — check your GITLAB_TOKEN",
        403: "forbidden — token lacks required permissions",
        404: "not found — variable or project does not exist",
    }
    detail = messages.get(status, f"HTTP {status}")
    die(detail)


def find_gitlab_file() -> Path:
    result = _find_gitlab_file_or_none()
    if result is None:
        die(".gitlab file not found in current or any parent directory")
    return result


def parse_repo_url(gitlab_file: Path) -> tuple[str, str]:
    """Returns (api_base_url, url-encoded project path)."""
    url = gitlab_file.read_text().strip()
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        die(f".gitlab contains invalid URL: {url!r}")
    host = f"{parsed.scheme}://{parsed.netloc}"
    project_path = parsed.path.lstrip("/")
    if project_path.endswith(".git"):
        project_path = project_path[:-4]
    return f"{host}/api/v4", quote(project_path, safe="")


def get_token() -> str:
    token = os.environ.get("GITLAB_TOKEN")
    if not token:
        die("GITLAB_TOKEN environment variable is not set")
    return token


def api_headers(token: str) -> dict:
    return {"PRIVATE-TOKEN": token}


CACHE_FILE = ".glc-cache"
TEMPLATE_FILE = ".glc-template.env"


def _parse_template(template_path: Path) -> list[str]:
    """Returns keys from the template in order (skips comments and blank lines)."""
    keys = []
    for line in template_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key = line.split("=", 1)[0]
            keys.append(key)
    return keys


def _parse_env_dict(env_path: Path) -> dict[str, str]:
    """Returns {KEY: 'KEY=value'} for non-comment, non-blank lines."""
    result = {}
    for line in env_path.read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            key = stripped.split("=", 1)[0]
            result[key] = stripped
    return result


def _reorder_env(env_path: Path, template_path: Path) -> str:
    """Returns env content structured like the template (comments/blanks preserved); extras appended."""
    env_dict = _parse_env_dict(env_path)
    used: set[str] = set()
    lines: list[str] = []
    for raw in template_path.read_text().splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            lines.append(raw)
        else:
            key = stripped.split("=", 1)[0]
            if key in env_dict:
                lines.append(env_dict[key])
                used.add(key)
            # missing keys are skipped (not added with empty value)
    extra = [line for key, line in env_dict.items() if key not in used]
    if extra:
        if lines and lines[-1] != "":
            lines.append("")
        lines.extend(extra)
    return "\n".join(lines) + "\n"


def _lint_env(env_path: Path, template_keys: list[str]) -> tuple[list[str], list[str]]:
    """Returns (missing, extra) keys relative to the template."""
    env_keys = set(_parse_env_dict(env_path).keys())
    template_set = set(template_keys)
    missing = [k for k in template_keys if k not in env_keys]
    extra = sorted(env_keys - template_set)
    return missing, extra


def _find_template(gitlab_file: Path) -> Path | None:
    """Returns .glc-template.env next to .gitlab, or None."""
    candidate = gitlab_file.parent / TEMPLATE_FILE
    return candidate if candidate.exists() else None


def _read_cache(gitlab_file: Path) -> list[str]:
    cache = gitlab_file.parent / CACHE_FILE
    if not cache.exists():
        return []
    return [line for line in cache.read_text().splitlines() if line.strip()]


def _write_cache(gitlab_file: Path, keys: list[str]) -> None:
    cache = gitlab_file.parent / CACHE_FILE
    cache.write_text("\n".join(sorted(keys)) + "\n")


def _complete_gitlab_keys(ctx, args, incomplete: str) -> list[str]:
    """Shell completion: read variable keys from local cache."""
    try:
        gitlab_file = _find_gitlab_file_or_none()
        if not gitlab_file:
            return []
        keys = _read_cache(gitlab_file)
        return [k for k in keys if k.startswith(incomplete)]
    except BaseException:
        return []


def _complete_local_envs(ctx, args, incomplete: str) -> list[str]:
    """Shell completion: list local .env files."""
    try:
        gitlab_file = _find_gitlab_file_or_none()
        if not gitlab_file:
            return []
        keys = [f.stem for f in sorted(gitlab_file.parent.glob("*.env"))]
        return [k for k in keys if k.startswith(incomplete)]
    except BaseException:
        return []


def _find_gitlab_file_or_none() -> Path | None:
    current = Path.cwd()
    while True:
        candidate = current / ".gitlab"
        if candidate.exists():
            return candidate
        parent = current.parent
        if parent == current:
            return None
        current = parent


@app.command(name="list")
def list_vars():
    """List all variable keys in the GitLab project."""
    gitlab_file = find_gitlab_file()
    api_base, project = parse_repo_url(gitlab_file)
    headers = api_headers(get_token())

    try:
        keys = []
        page = 1
        while True:
            resp = requests.get(
                f"{api_base}/projects/{project}/variables",
                headers=headers,
                params={"per_page": 100, "page": page},
            )
            resp.raise_for_status()
            data = resp.json()
            if not data:
                break
            keys.extend(var["key"] for var in data)
            if len(data) < 100:
                break
            page += 1
    except requests.HTTPError as e:
        handle_http_error(e)
    except requests.ConnectionError:
        die(f"could not connect to {api_base}")

    if not keys:
        typer.echo("no variables found")
        return

    _write_cache(gitlab_file, keys)
    for key in sorted(keys):
        typer.echo(key)


@app.command()
def pull(env_name: str = typer.Argument(..., help="Variable key in GitLab (e.g. VN-APP-PROD-01)", autocompletion=_complete_gitlab_keys)):
    """Pull a file-type variable from GitLab and write it to a local file."""
    gitlab_file = find_gitlab_file()
    api_base, project = parse_repo_url(gitlab_file)
    headers = api_headers(get_token())

    try:
        resp = requests.get(
            f"{api_base}/projects/{project}/variables/{env_name}",
            headers=headers,
        )
        resp.raise_for_status()
    except requests.HTTPError as e:
        handle_http_error(e)
    except requests.ConnectionError:
        die(f"could not connect to {api_base}")

    value = resp.json()["value"]
    env_file = gitlab_file.parent / f"{env_name}.env"
    env_file.write_text(value)
    ok(f"pulled {env_name} -> {env_file.name}")


@app.command()
def lint(
    env_name: str = typer.Argument(None, help="Variable key (e.g. VN-APP-PROD-01); omit to check all *.env files", autocompletion=_complete_local_envs),
):
    """Lint local .env file(s) against the template."""
    gitlab_file = find_gitlab_file()
    template_path = _find_template(gitlab_file)
    if template_path is None:
        die(f"{TEMPLATE_FILE} not found next to .gitlab")

    template_keys = _parse_template(template_path)

    if env_name:
        env_files = [gitlab_file.parent / f"{env_name}.env"]
        if not env_files[0].exists():
            die(f"{env_name}.env not found")
    else:
        env_files = sorted(gitlab_file.parent.glob("*.env"))
        if not env_files:
            typer.echo("no .env files found")
            return

    any_missing = False
    for env_file in env_files:
        missing, extra = _lint_env(env_file, template_keys)
        name = env_file.stem
        if not missing and not extra:
            ok(f"{name}: ok")
            continue
        typer.echo(typer.style(f"{name}:", bold=True))
        for key in missing:
            typer.echo(typer.style(f"  missing: {key}", fg=typer.colors.RED))
        for key in extra:
            typer.echo(typer.style(f"  extra:   {key}", fg=typer.colors.YELLOW))
        if missing:
            any_missing = True

    if any_missing:
        raise typer.Exit(1)


def _save_backup(gitlab_file: Path, env_name: str, value: str) -> Path:
    backup_dir = gitlab_file.parent / ".glc-backups"
    backup_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = backup_dir / f"{env_name}.env.{timestamp}"
    backup_file.write_text(value)
    return backup_file


def _show_diff(old: str, new: str, env_name: str) -> bool:
    """Print colored unified diff. Returns True if there are changes."""
    diff = list(difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"{env_name} (remote)",
        tofile=f"{env_name} (local)",
    ))
    if not diff:
        return False
    for line in diff:
        line = line.rstrip()
        if line.startswith("+") and not line.startswith("+++"):
            typer.echo(typer.style(line, fg=typer.colors.GREEN))
        elif line.startswith("-") and not line.startswith("---"):
            typer.echo(typer.style(line, fg=typer.colors.RED))
        else:
            typer.echo(line)
    return True


@app.command()
def push(
    env_name: str = typer.Argument(..., help="Variable key in GitLab (e.g. VN-APP-PROD-01)", autocompletion=_complete_local_envs),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
):
    """Push a local file to GitLab as a file-type variable (create or update)."""
    gitlab_file = find_gitlab_file()
    api_base, project = parse_repo_url(gitlab_file)
    headers = api_headers(get_token())

    env_file = gitlab_file.parent / f"{env_name}.env"
    if not env_file.exists():
        die(f"{env_name}.env not found")

    local_value = env_file.read_text()

    template_path = _find_template(gitlab_file)
    if template_path is not None:
        template_keys = _parse_template(template_path)
        missing, extra = _lint_env(env_file, template_keys)
        if missing:
            typer.echo(typer.style("warning: ", fg=typer.colors.YELLOW, bold=True) + "missing keys in " + env_file.name + ":")
            for key in missing:
                typer.echo(f"  {key}")
            if not yes and not typer.confirm("continue anyway?"):
                typer.echo("aborted")
                raise typer.Exit(0)
        reordered = _reorder_env(env_file, template_path)
        if reordered != local_value:
            env_file.write_text(reordered)
            local_value = reordered

    try:
        check = requests.get(
            f"{api_base}/projects/{project}/variables/{env_name}",
            headers=headers,
        )

        if check.status_code == 200:
            remote_value = check.json()["value"]
            has_changes = _show_diff(remote_value, local_value, env_name)

            if not has_changes:
                ok(f"{env_name} is already up to date")
                return

            if not yes and not typer.confirm("\npush these changes?"):
                typer.echo("aborted")
                raise typer.Exit(0)

            backup = _save_backup(gitlab_file, env_name, remote_value)
            requests.put(
                f"{api_base}/projects/{project}/variables/{env_name}",
                headers=headers,
                json={"value": local_value, "variable_type": "file"},
            ).raise_for_status()
            ok(f"updated {env_name}  (backup saved to {backup.name})")

        elif check.status_code == 404:
            if not yes and not typer.confirm(f"create new variable {env_name}?"):
                typer.echo("aborted")
                raise typer.Exit(0)

            requests.post(
                f"{api_base}/projects/{project}/variables",
                headers=headers,
                json={"key": env_name, "value": local_value, "variable_type": "file"},
            ).raise_for_status()
            ok(f"created {env_name}")
        else:
            check.raise_for_status()
    except requests.HTTPError as e:
        handle_http_error(e)
    except requests.ConnectionError:
        die(f"could not connect to {api_base}")


@app.command()
def ui():
    """Launch interactive TUI."""
    from glc.tui import GlcApp

    GlcApp().run()


def main():
    app()


if __name__ == "__main__":
    main()
