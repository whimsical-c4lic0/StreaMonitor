"""Offline integration coverage for the default server-rendered web skin."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from bs4 import BeautifulSoup
from flask import Flask
import pytest

from streamonitor.enums import Status
from streamonitor.manager import Manager
from streamonitor.managers.httpmanager import httpmanager as module


class FakeStreamer:
    site = "Chaturbate"
    siteslug = "CB"
    country_data = SimpleNamespace(name="Unknown", flag="")
    gender_data = SimpleNamespace(name="Unknown")
    last_stream = 0

    def __init__(self, username="example_streamer", running=True, recording=False, videos=None):
        self.username = username
        self.url = f"https://example.invalid/{username}"
        self.running = running
        self.recording = recording
        self.sc = Status.PUBLIC if recording else Status.OFFLINE
        self.video_files = videos or []
        self.video_files_total_size = sum(v.filesize for v in self.video_files)

    def cache_file_list(self):
        self.video_files = [v for v in self.video_files if Path(v.abs_path).exists()]
        self.video_files_total_size = sum(v.filesize for v in self.video_files)

    def is_alive(self):
        return True

    def restart(self):
        self.running = True

    def stop(self, *_):
        self.running = False


def make_test_app(monkeypatch, tmp_path):
    monkeypatch.setattr(module, "WEBSERVER_SKIN", "truck-kun")
    monkeypatch.setattr(module, "WEBSERVER_PASSWORD", "")
    monkeypatch.setattr(module, "WEB_LIST_FREQUENCY", 60)
    monkeypatch.setattr(module.OOSDetector, "space_usage", lambda: SimpleNamespace(free=4000000000, total=10000000000))
    files = []
    for index in (1, 2):
        filename = f"example_streamer-20261123-12000{index}.mp4"
        path = tmp_path / filename
        path.write_bytes(b"test video")
        files.append(
            SimpleNamespace(
                filename=filename,
                shortname=filename,
                filesize=index * 1000,
                human_readable_filesize=f"{index} KB",
                mimetype="video/mp4",
                abs_path=str(path),
            )
        )
    streamer = FakeStreamer(videos=files)
    streamer.outputFolder = str(tmp_path)
    manager = module.HTTPManager([streamer])
    manager.loaded_site_names = ["Chaturbate", "StripChat"]
    monkeypatch.setattr(
        manager,
        "getStreamer",
        lambda user, site: next((s for s in manager.streamers if s.username == user and s.site == site), None),
    )
    monkeypatch.setattr(manager, "saveConfig", lambda: None)

    def add(existing, user, site, start_monitoring=True):
        if existing:
            return "Streamer already exists"
        if user == "failed_streamer":
            return "Failed to add: simulated failure"
        manager.streamers.append(FakeStreamer(user, running=start_monitoring))
        return f"Added [CB] {user}"

    monkeypatch.setattr(manager, "do_add", add)
    captured = []
    monkeypatch.setattr(Flask, "run", lambda app, **_: captured.append(app))
    manager.run()
    app = captured[0]
    app.config.update(TESTING=True)
    return app, manager


@pytest.fixture
def ui(monkeypatch, tmp_path):
    return make_test_app(monkeypatch, tmp_path)


def soup(response):
    assert response.status_code == 200, response.get_data(as_text=True)
    return BeautifulSoup(response.data, "html.parser")


def test_home_has_accessible_drawer_filters_and_real_summary(ui):
    app, manager = ui
    page = soup(app.test_client().get("/"))
    assert page.h1.text == "Streamers"
    assert page.select_one("#add-dialog[aria-labelledby='add-title']")
    assert page.select_one("input[name=start_monitoring][checked]")
    assert page.select_one("#username[minlength='3'][required]")
    assert len(page.select("#summary")) == 1
    assert [x.text for x in page.select("#summary strong")] == ["0", "1", "1"]
    assert not page.select("aside")
    assert page.select_one(".streamer-name")["href"] == "/recordings/example_streamer/Chaturbate"
    assert page.select_one(".streamer-more button[hx-delete]")["hx-confirm"]


def test_refresh_filters_sorting_and_oob_summary(ui):
    app, manager = ui
    client = app.test_client()
    page = soup(client.get("/refresh/streamers?filter-username=missing"))
    assert "No matching streamers" in page.text
    assert page.select_one("#summary[hx-swap-oob='outerHTML']")
    manager.streamers.append(FakeStreamer("zzz_streamer", recording=True))
    page = soup(client.get("/refresh/streamers?sort_by=username&sort_dir=desc"))
    assert page.select_one(".streamer-name").text == "zzz_streamer"
    assert [x.text for x in page.select("#summary strong")] == ["1", "2", "1"]
    page = soup(client.get("/refresh/streamers?filter-status=rec"))
    assert len(page.select(".streamer")) == 1


@pytest.mark.parametrize("enabled", [True, False])
def test_add_respects_monitoring_checkbox(ui, enabled):
    app, manager = ui
    data = {"username": "new_streamer", "site": "Chaturbate"}
    if enabled:
        data["start_monitoring"] = "1"
    page = soup(app.test_client().post("/add", data=data))
    assert manager.streamers[-1].running is enabled
    assert "Added [CB] new_streamer" in page.text
    assert page.select_one("#summary[hx-swap-oob]")


@pytest.mark.parametrize(
    "username, site",
    [
        ("ab", "Chaturbate"),
        ("validname", "unknown"),
        ("https://room", "Chaturbate"),
        ("example_streamer", "Chaturbate"),
        ("failed_streamer", "Chaturbate"),
    ],
)
def test_add_errors_are_non_success_responses(ui, username, site):
    app, manager = ui
    response = app.test_client().post("/add", data={"username": username, "site": site})
    assert response.status_code == 500
    assert BeautifulSoup(response.data, "html.parser").select_one(".toast.error")
    assert len(manager.streamers) == 1


def test_recordings_native_player_selected_row_and_confirmation(ui):
    app, manager = ui
    page = soup(app.test_client().get("/recordings/example_streamer/Chaturbate"))
    assert page.select_one("video[controls][playsinline]")
    assert page.select_one(".player-actions a[download]")
    assert page.select_one(".file.selected .file-info[aria-current='true']")
    assert page.select_one(".file.selected button[disabled]")
    assert len(page.select(".file")) == 2
    assert all("Permanently delete" in b["hx-confirm"] for b in page.select(".remove-video"))
    assert len(page.select("#toggle-streamer")) == 1
    assert page.select_one(".back-link")["href"] == "/"
    assert page.select_one("#nav-bar[hx-get]")


def test_single_selected_file_cannot_be_deleted_in_ui(ui):
    app, manager = ui
    manager.streamers[0].video_files.pop()
    page = soup(app.test_client().get("/recordings/example_streamer/Chaturbate"))
    assert page.select_one(".file.selected button[disabled]")


def test_watch_sort_delete_and_pause_resume(ui):
    app, manager = ui
    client = app.test_client()
    streamer = manager.streamers[0]
    filename = streamer.video_files[0].filename
    response = client.get(f"/videos/watch/example_streamer/Chaturbate/{filename}")
    page = soup(response)
    assert page.select_one(".file.selected .filename").text.strip().endswith(filename)
    assert "play_video=" in response.headers["HX-Replace-Url"]
    page = soup(client.get(f"/videos/example_streamer/Chaturbate?sorted=True&play_video={filename}"))
    assert page.select_one(".file .filename").text.strip().endswith(streamer.video_files[1].filename)
    assert page.select_one(".sorted-context")
    page = soup(client.delete(f"/videos/example_streamer/Chaturbate/{filename}"))
    assert len(page.select(".file")) == 1
    page = soup(client.patch("/toggle/example_streamer/Chaturbate/recording"))
    assert "Resume monitoring" in page.text
    assert not streamer.running
    page = soup(client.patch("/toggle/example_streamer/Chaturbate/recording"))
    assert "Pause monitoring" in page.text
    assert streamer.running


@pytest.mark.parametrize("running", [False, True])
def test_recordings_empty_state_keeps_monitoring_control(ui, running):
    app, manager = ui
    manager.streamers[0].video_files = []
    manager.streamers[0].running = running
    page = soup(app.test_client().get("/recordings/example_streamer/Chaturbate"))
    assert not page.select("video")
    assert "No recordings yet" in page.text
    assert page.select_one("#toggle-streamer")
    assert ("Monitoring is enabled" if running else "Monitoring is paused") in page.text


@pytest.mark.parametrize("enabled", [False, True])
def test_manager_add_only_restarts_when_monitoring_requested(monkeypatch, enabled):
    bot = Mock(siteslug="CB", username="new_streamer")
    monkeypatch.setattr(module.Bot, "createInstance", lambda *_: bot)
    manager = Manager([])
    monkeypatch.setattr(manager, "saveConfig", Mock())
    result = manager.do_add(None, "new_streamer", "Chaturbate", start_monitoring=enabled)
    assert result == "Added [CB] new_streamer"
    bot.start.assert_called_once()
    assert bot.restart.call_count == int(enabled)
    manager.saveConfig.assert_called_once()


def test_template_files_compile(ui):
    app, _ = ui
    for name in app.jinja_env.list_templates():
        app.jinja_env.get_template(name)


@pytest.mark.parametrize("key", ["username", "status", "last_stream", "video_files_total_size", "running"])
def test_column_headings_toggle_sort_and_preserve_state(ui, key):
    app, manager = ui
    first = manager.streamers[0]
    first.last_stream = 100
    second = FakeStreamer("zzz_streamer", running=False, recording=True)
    second.last_stream = 200
    manager.streamers.append(second)
    client = app.test_client()
    page = soup(client.get("/"))
    assert not page.select("select[name=sort_by], select[name=sort_dir]")

    def heading(page):
        return next(
            button for button in page.select("th .column-sort") if json.loads(button["hx-vals"])["sort_by"] == key
        )

    # A different column starts ascending; the active column reverses direction.
    params = json.loads(heading(page)["hx-vals"])
    assert params["sort_dir"] == ("desc" if key == "username" else "asc")
    orders = []
    for _ in range(2):
        button = heading(page)
        assert button["type"] == "button"
        assert button["hx-include"] == ".filter-streamers-state"
        params = json.loads(button["hx-vals"])
        page = soup(client.get(button["hx-get"], query_string=params))
        assert heading(page).parent["aria-sort"] == ("ascending" if params["sort_dir"] == "asc" else "descending")
        assert len(page.select("th[aria-sort]")) == 1
        assert page.select_one("#sort_by")["value"] == key
        assert page.select_one("#sort_dir")["value"] == params["sort_dir"]
        orders.append([name.text for name in page.select(".streamer-name")])
    assert orders[0] == list(reversed(orders[1]))

    # Cookies and hidden inputs keep the heading state on reloads and refreshes.
    page = soup(client.get("/"))
    assert page.select_one("#sort_by")["value"] == key
    state = {field["name"]: field["value"] for field in page.select("input[type=hidden].filter-streamers-state")}
    page = soup(client.get("/refresh/streamers", query_string={**state, "filter-username": "zzz"}))
    assert [name.text for name in page.select(".streamer-name")] == ["zzz_streamer"]
    assert page.select_one("#sort_by")["value"] == key
    assert json.loads(heading(page)["hx-vals"])["sort_dir"] != state["sort_dir"]
