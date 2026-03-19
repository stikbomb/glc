"""Textual TUI for glc — interactive GitLab variable manager."""
from __future__ import annotations

import difflib
import os
from pathlib import Path

import requests
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Footer, Header, Label, ListItem, ListView, RichLog, Static, TextArea


# ─── Sync-aware RichLog ───────────────────────────────────────────────────────


class SyncableLog(RichLog):
    """RichLog that posts a Scrolled message when scroll_y changes."""

    class Scrolled(Message):
        def __init__(self, log: "SyncableLog", y: float) -> None:
            super().__init__()
            self.log = log
            self.y = y

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        self.post_message(self.Scrolled(self, new_value))


# ─── Messages ────────────────────────────────────────────────────────────────


class RemoteFetched(Message):
    def __init__(self, env_name: str, remote_value: str) -> None:
        super().__init__()
        self.env_name = env_name
        self.remote_value = remote_value


class FetchFailed(Message):
    def __init__(self, env_name: str, error: str) -> None:
        super().__init__()
        self.env_name = env_name
        self.error = error


class PushComplete(Message):
    def __init__(self, success: bool, message: str) -> None:
        super().__init__()
        self.success = success
        self.message = message


class PullComplete(Message):
    def __init__(self, success: bool, message: str) -> None:
        super().__init__()
        self.success = success
        self.message = message


# ─── Confirm Modal ───────────────────────────────────────────────────────────


class ConfirmModal(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "yes", "Yes"),
        Binding("n", "no", "No"),
        Binding("escape", "no", "Cancel"),
    ]

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self._prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label(self._prompt)
            with Horizontal(id="confirm-buttons"):
                yield Button("Yes [y]", id="yes-btn", variant="success")
                yield Button("No [n]", id="no-btn", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes-btn")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


# ─── Edit Modal ──────────────────────────────────────────────────────────────


class EditModal(ModalScreen[str | None]):
    BINDINGS = [
        Binding("ctrl+s", "save", "Save"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, content: str, env_name: str) -> None:
        super().__init__()
        self._content = content
        self._env_name = env_name

    def compose(self) -> ComposeResult:
        with Vertical(id="edit-dialog"):
            yield Label(f"  {self._env_name}.env  [ctrl+s] save  [esc] cancel", id="edit-title")
            yield TextArea(id="edit-area")

    def on_mount(self) -> None:
        area = self.query_one("#edit-area", TextArea)
        area.load_text(self._content)
        area.focus()

    def action_save(self) -> None:
        self.dismiss(self.query_one("#edit-area", TextArea).text)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ─── Env List Pane ───────────────────────────────────────────────────────────


class EnvListPane(Widget):
    def compose(self) -> ComposeResult:
        yield Label(".env files", id="env-list-title")
        yield ListView(id="env-list")

    def update_list(self, env_files: list[Path]) -> None:
        lv = self.query_one(ListView)
        lv.clear()
        for f in env_files:
            lv.append(ListItem(Label(f.stem), name=f.stem))

    def show_message(self, text: str) -> None:
        self.query_one(ListView).clear()
        self.query_one("#env-list-title", Label).update(text)


# ─── Diff Pane ───────────────────────────────────────────────────────────────


class DiffPane(Widget):
    def compose(self) -> ComposeResult:
        with Horizontal(id="diff-cols-header"):
            yield Label("  LOCAL", id="local-title")
            yield Label("", id="center-header")
            yield Label("  REMOTE", id="remote-title")
        with Horizontal(id="diff-cols"):
            yield SyncableLog(id="local-log", highlight=False, markup=True)
            with Vertical(id="diff-actions"):
                yield Static("●", id="sync-lamp")
                yield Button("→ push", id="push-btn", variant="success", disabled=True)
                yield Button("← pull", id="pull-btn", variant="primary", disabled=True)
                yield Button("✎ edit", id="edit-btn", variant="default")
                yield Button("⇕ sync", id="sync-btn", variant="default")
                yield Button("⟳ reload", id="reload-btn", variant="default")
            yield SyncableLog(id="remote-log", highlight=False, markup=True)
        yield Static("", id="diff-status")

    _sync_scroll: bool = False

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._sync_pending: dict[int, int] = {}

    @property
    def _local(self) -> SyncableLog:
        return self.query_one("#local-log", SyncableLog)

    @property
    def _remote(self) -> SyncableLog:
        return self.query_one("#remote-log", SyncableLog)

    def on_syncable_log_scrolled(self, event: SyncableLog.Scrolled) -> None:
        if not self._sync_scroll:
            return
        log_id = id(event.log)
        pending = self._sync_pending.get(log_id, 0)
        if pending > 0:
            self._sync_pending[log_id] = pending - 1
            return
        target = self._remote if event.log is self._local else self._local
        target_id = id(target)
        self._sync_pending[target_id] = self._sync_pending.get(target_id, 0) + 1
        target.scroll_to(y=event.y, animate=False, immediate=True)

    def toggle_sync(self) -> None:
        self._sync_scroll = not self._sync_scroll
        self.query_one("#sync-btn", Button).variant = (
            "success" if self._sync_scroll else "error"
        )

    def show_loading(self) -> None:
        self._local.clear()
        self._remote.clear()
        self._local.loading = True
        self._remote.loading = True
        self.query_one("#push-btn", Button).disabled = True
        self.query_one("#pull-btn", Button).disabled = True
        self.query_one("#diff-status", Static).update("")

    def show_diff(self, local_text: str, remote_text: str, preserve_scroll: bool = False) -> None:
        local_y = self._local.scroll_y if preserve_scroll else 0
        remote_y = self._remote.scroll_y if preserve_scroll else 0

        self._local.loading = False
        self._remote.loading = False
        self._local.clear()
        self._remote.clear()

        a = local_text.splitlines()
        b = remote_text.splitlines()
        matcher = difflib.SequenceMatcher(None, a, b)

        n_added = 0
        n_removed = 0

        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                for line in a[i1:i2]:
                    self._local.write(line)
                    self._remote.write(line)
            elif tag == "delete":
                for line in a[i1:i2]:
                    self._local.write(f"[green]+{line}[/green]")
                    self._remote.write("")
                n_added += i2 - i1
            elif tag == "insert":
                for line in b[j1:j2]:
                    self._local.write("")
                    self._remote.write(f"[red]-{line}[/red]")
                n_removed += j2 - j1
            elif tag == "replace":
                local_chunk = a[i1:i2]
                remote_chunk = b[j1:j2]
                max_len = max(len(local_chunk), len(remote_chunk))
                for line in local_chunk:
                    self._local.write(f"[green]+{line}[/green]")
                for _ in range(max_len - len(local_chunk)):
                    self._local.write("")
                for line in remote_chunk:
                    self._remote.write(f"[red]-{line}[/red]")
                for _ in range(max_len - len(remote_chunk)):
                    self._remote.write("")
                n_added += len(local_chunk)
                n_removed += len(remote_chunk)

        has_diff = n_added > 0 or n_removed > 0
        self.query_one("#push-btn", Button).disabled = not has_diff
        self.query_one("#pull-btn", Button).disabled = not has_diff
        self.query_one("#sync-lamp", Static).update(
            "[red]●[/red]" if has_diff else "[green]●[/green]"
        )

        if has_diff:
            parts = []
            if n_added:
                parts.append(f"{n_added} added")
            if n_removed:
                parts.append(f"{n_removed} removed")
            self.query_one("#diff-status", Static).update("  " + ", ".join(parts))
        else:
            self.query_one("#diff-status", Static).update("  up to date")

        if preserve_scroll:
            if local_y:
                self._local.scroll_to(y=local_y, animate=False)
            if remote_y:
                self._remote.scroll_to(y=remote_y, animate=False)

    def show_error(self, text: str) -> None:
        self._local.loading = False
        self._remote.loading = False
        self._local.clear()
        self._remote.clear()
        self.query_one("#sync-lamp", Static).update("[red]●[/red]")
        self.query_one("#diff-status", Static).update(f"  [red]{text}[/red]")
        self.query_one("#push-btn", Button).disabled = True
        self.query_one("#pull-btn", Button).disabled = True

    def set_status(self, text: str, ok: bool = True) -> None:
        colour = "green" if ok else "red"
        self.query_one("#diff-status", Static).update(f"  [{colour}]{text}[/{colour}]")


# ─── Main App ─────────────────────────────────────────────────────────────────


class GlcApp(App):
    CSS = """
    Screen {
        layout: vertical;
    }

    #main {
        layout: horizontal;
        height: 1fr;
    }

    EnvListPane {
        width: 22;
        border-right: solid $accent;
        padding: 0 1;
    }

    #env-list-title {
        padding: 0 0 1 0;
        text-style: bold;
    }

    DiffPane {
        width: 1fr;
    }

    #diff-cols-header {
        height: 1;
        background: $surface-darken-1;
    }

    #local-title, #remote-title {
        width: 1fr;
        text-style: bold;
    }

    #center-header {
        width: 14;
    }

    #diff-cols {
        height: 1fr;
    }

    #local-log {
        width: 1fr;
    }

    #remote-log {
        width: 1fr;
    }

    #diff-actions {
        width: 14;
        border-left: solid $accent;
        border-right: solid $accent;
        align: center middle;
        padding: 1 0;
    }

    #sync-lamp {
        width: 100%;
        height: 1;
        text-align: center;
        margin-bottom: 1;
    }

    #diff-actions Button {
        width: 100%;
        margin: 0 0 1 0;
    }

    #diff-status {
        height: 1;
        color: $text-muted;
    }

    ConfirmModal {
        align: center middle;
    }

    #confirm-dialog {
        width: 60;
        height: auto;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }

    #confirm-buttons {
        height: 3;
        align: center middle;
        margin-top: 1;
    }

    #confirm-buttons Button {
        margin: 0 1;
    }

    EditModal {
        align: center middle;
    }

    #edit-dialog {
        width: 90%;
        height: 90%;
        border: round $accent;
        background: $surface;
    }

    #edit-title {
        height: 1;
        background: $surface-darken-1;
        padding: 0 1;
    }

    #edit-area {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("e", "edit", "Edit"),
        Binding("s", "sync_scroll", "Sync scroll"),
    ]

    TITLE = "glc"

    def __init__(self) -> None:
        super().__init__()
        from glc.cli import _find_gitlab_file_or_none, parse_repo_url, api_headers, _save_backup

        self._find_file = _find_gitlab_file_or_none
        self._parse_url = parse_repo_url
        self._make_headers = api_headers
        self._save_bak = _save_backup

        self._gitlab_file: Path | None = None
        self._current_env: str | None = None
        self._remote_cache: dict[str, str] = {}
        self._area_env: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main"):
            yield EnvListPane(id="env-list-pane")
            yield DiffPane(id="diff-pane")
        yield Footer()

    def on_mount(self) -> None:
        self._load_env_list()

    def _load_env_list(self) -> None:
        self._gitlab_file = self._find_file()
        pane = self.query_one(EnvListPane)
        if self._gitlab_file is None:
            pane.show_message("[red].gitlab not found[/red]")
            return
        env_files = sorted(self._gitlab_file.parent.glob("*.env"))
        if not env_files:
            pane.show_message("[yellow]no .env files[/yellow]")
            return
        pane.update_list(env_files)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.item is None:
            return
        env_name = event.item.name
        if not env_name:
            return
        self._current_env = env_name
        self._load_diff(env_name)

    def _load_diff(self, env_name: str) -> None:
        if env_name in self._remote_cache:
            self._render_diff(env_name, self._remote_cache[env_name])
            return
        self.query_one(DiffPane).show_loading()
        self._fetch_worker(env_name)

    def _render_diff(self, env_name: str, remote: str) -> None:
        if self._gitlab_file is None:
            return
        env_file = self._gitlab_file.parent / f"{env_name}.env"
        local = env_file.read_text() if env_file.exists() else ""
        preserve = env_name == self._area_env
        self._area_env = env_name
        self.query_one(DiffPane).show_diff(local, remote, preserve_scroll=preserve)

    # ─── Edit action ─────────────────────────────────────────────────────────

    def action_edit(self) -> None:
        env = self._current_env
        if not env or not self._gitlab_file:
            return
        env_file = self._gitlab_file.parent / f"{env}.env"
        content = env_file.read_text() if env_file.exists() else ""

        def done(result: str | None) -> None:
            if result is not None:
                env_file.write_text(result)
                self._remote_cache.pop(env, None)
                self._load_diff(env)

        self.push_screen(EditModal(content, env), done)

    # ─── HTTP workers ────────────────────────────────────────────────────────

    @work(thread=True)
    def _fetch_worker(self, env_name: str) -> None:
        token = os.environ.get("GITLAB_TOKEN")
        if not token:
            self.post_message(FetchFailed(env_name, "GITLAB_TOKEN not set"))
            return
        gitlab_file = self._gitlab_file
        if gitlab_file is None:
            self.post_message(FetchFailed(env_name, ".gitlab not found"))
            return
        try:
            api_base, project = self._parse_url(gitlab_file)
            resp = requests.get(
                f"{api_base}/projects/{project}/variables/{env_name}",
                headers=self._make_headers(token),
                timeout=15,
            )
            resp.raise_for_status()
            self.post_message(RemoteFetched(env_name, resp.json()["value"]))
        except BaseException as exc:
            self.post_message(FetchFailed(env_name, str(exc)))

    def on_remote_fetched(self, event: RemoteFetched) -> None:
        self._remote_cache[event.env_name] = event.remote_value
        if event.env_name == self._current_env:
            self._render_diff(event.env_name, event.remote_value)

    def on_fetch_failed(self, event: FetchFailed) -> None:
        if event.env_name == self._current_env:
            self.query_one(DiffPane).show_error(event.error)

    # ─── Button actions ──────────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "push-btn":
            self._confirm_push()
        elif event.button.id == "pull-btn":
            self._confirm_pull()
        elif event.button.id == "edit-btn":
            self.action_edit()
        elif event.button.id == "sync-btn":
            self.action_sync_scroll()
        elif event.button.id == "reload-btn":
            self.action_refresh()

    def _confirm_push(self) -> None:
        env = self._current_env
        if not env:
            return

        def done(result: bool) -> None:
            if result:
                self._push_worker(env)

        self.push_screen(ConfirmModal(f"Push local → remote: {env}?"), done)

    def _confirm_pull(self) -> None:
        env = self._current_env
        if not env:
            return

        def done(result: bool) -> None:
            if result:
                self._pull_worker(env)

        self.push_screen(ConfirmModal(f"Pull remote → local: {env}?"), done)

    @work(thread=True)
    def _push_worker(self, env_name: str) -> None:
        token = os.environ.get("GITLAB_TOKEN")
        if not token:
            self.post_message(PushComplete(False, "GITLAB_TOKEN not set"))
            return
        gitlab_file = self._gitlab_file
        if gitlab_file is None:
            self.post_message(PushComplete(False, ".gitlab not found"))
            return
        try:
            api_base, project = self._parse_url(gitlab_file)
            headers = self._make_headers(token)
            env_file = gitlab_file.parent / f"{env_name}.env"
            local_value = env_file.read_text()

            check = requests.get(
                f"{api_base}/projects/{project}/variables/{env_name}",
                headers=headers,
                timeout=15,
            )
            if check.status_code == 200:
                self._save_bak(gitlab_file, env_name, check.json()["value"])
                requests.put(
                    f"{api_base}/projects/{project}/variables/{env_name}",
                    headers=headers,
                    json={"value": local_value, "variable_type": "file"},
                    timeout=15,
                ).raise_for_status()
                msg = f"updated {env_name}"
            elif check.status_code == 404:
                requests.post(
                    f"{api_base}/projects/{project}/variables",
                    headers=headers,
                    json={"key": env_name, "value": local_value, "variable_type": "file"},
                    timeout=15,
                ).raise_for_status()
                msg = f"created {env_name}"
            else:
                check.raise_for_status()
                msg = ""  # unreachable
            self._remote_cache.pop(env_name, None)
            self.post_message(PushComplete(True, msg))
        except BaseException as exc:
            self.post_message(PushComplete(False, str(exc)))

    @work(thread=True)
    def _pull_worker(self, env_name: str) -> None:
        token = os.environ.get("GITLAB_TOKEN")
        if not token:
            self.post_message(PullComplete(False, "GITLAB_TOKEN not set"))
            return
        gitlab_file = self._gitlab_file
        if gitlab_file is None:
            self.post_message(PullComplete(False, ".gitlab not found"))
            return
        try:
            api_base, project = self._parse_url(gitlab_file)
            resp = requests.get(
                f"{api_base}/projects/{project}/variables/{env_name}",
                headers=self._make_headers(token),
                timeout=15,
            )
            resp.raise_for_status()
            value = resp.json()["value"]
            env_file = gitlab_file.parent / f"{env_name}.env"
            env_file.write_text(value)
            self._remote_cache.pop(env_name, None)
            self.post_message(PullComplete(True, f"pulled {env_name}"))
        except BaseException as exc:
            self.post_message(PullComplete(False, str(exc)))

    def on_push_complete(self, event: PushComplete) -> None:
        pane = self.query_one(DiffPane)
        pane.set_status(event.message, ok=event.success)
        if event.success and self._current_env:
            self._fetch_worker(self._current_env)

    def on_pull_complete(self, event: PullComplete) -> None:
        pane = self.query_one(DiffPane)
        pane.set_status(event.message, ok=event.success)
        if event.success and self._current_env:
            self._fetch_worker(self._current_env)

    def action_sync_scroll(self) -> None:
        self.query_one(DiffPane).toggle_sync()

    def action_refresh(self) -> None:
        self._remote_cache.clear()
        self._load_env_list()
        if self._current_env:
            self._load_diff(self._current_env)
