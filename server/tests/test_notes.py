"""The markdown vault: indexing, ranking, the graph, and capture.

The case that matters most here is the node index. Search returns positions into
the graph's node array so the viewer can fly the camera to a note without a
second lookup, and if those two ever disagree the dive lands on an unrelated
star — a wrong answer that looks exactly like a right one.
"""

from __future__ import annotations

import pytest

from jarvis import notes as notes_mod
from jarvis.notes import NotesError, Vault


@pytest.fixture
def vault(tmp_path, monkeypatch):
    (tmp_path / "projects").mkdir()
    (tmp_path / "people").mkdir()
    (tmp_path / ".obsidian").mkdir()

    (tmp_path / "projects" / "Jarvis.md").write_text(
        "---\ntags: [build]\n---\n\nSelf-hosted assistant on Oracle Cloud. "
        "Runs behind [[Caddy]] and talks to Groq.\n"
    )
    (tmp_path / "projects" / "Caddy.md").write_text("Reverse proxy, does TLS on its own.\n")
    (tmp_path / "people" / "Zubair.md").write_text("Wrote the prompt pack about knowledge galaxies.\n")
    (tmp_path / ".obsidian" / "workspace.md").write_text("not a note\n")

    monkeypatch.setattr(notes_mod, "notes_dir", lambda: tmp_path)
    return Vault()


def test_obsidian_state_is_not_indexed(vault):
    titles = {n.title for n in vault.load()}
    assert titles == {"Jarvis", "Caddy", "Zubair"}


def test_frontmatter_is_stripped_from_the_body(vault):
    jarvis = next(n for n in vault.load() if n.title == "Jarvis")
    assert "tags:" not in jarvis.text
    assert jarvis.text.startswith("Self-hosted assistant")


def test_a_title_match_outranks_a_passing_mention(vault):
    # "Caddy" is a title and also appears inside the Jarvis note. Without the
    # title weighting the longer note wins on surface area alone.
    hits = vault.search("caddy")
    assert hits[0][0].title == "Caddy"


def test_search_reports_the_index_the_graph_uses(vault):
    graph = vault.graph()
    for note, _score in vault.search("caddy proxy"):
        assert graph["nodes"][note.index]["label"] == note.title


def test_a_wikilink_becomes_an_edge(vault):
    graph = vault.graph()
    by_label = {n["label"]: n["id"] for n in graph["nodes"]}
    pair = tuple(sorted((by_label["Jarvis"], by_label["Caddy"])))
    edges = {tuple(sorted((link["source"], link["target"]))) for link in graph["links"]}
    assert pair in edges


def test_a_note_is_never_linked_to_itself(vault):
    for link in vault.graph()["links"]:
        assert link["source"] != link["target"]


def test_a_question_with_only_stopwords_matches_nothing(vault):
    assert vault.search("what is the") == []


def test_capture_writes_a_note_and_reindexes(vault, tmp_path):
    note = vault.capture("Remember that the Oracle box reboots on Sundays")
    written = tmp_path / "captures" / f"{note.title}.md"

    assert written.exists()
    assert "source: jarvis" in written.read_text()
    # It has to be searchable straight away — the star should appear without
    # waiting for the next reload.
    assert any(n.title == note.title for n in vault.load())
    assert vault.search("oracle reboots")


def test_capturing_the_same_words_twice_keeps_both(vault):
    first = vault.capture("Standing note about Fridays")
    second = vault.capture("Standing note about Fridays")
    assert first.title != second.title


def test_a_title_that_is_illegal_as_a_filename_still_writes(vault, tmp_path):
    note = vault.capture("Ratio: profit / loss <target> for Q3")
    assert (tmp_path / "captures" / f"{note.title}.md").exists()


def test_capture_without_a_vault_says_what_to_set(monkeypatch):
    monkeypatch.setattr(notes_mod, "notes_dir", lambda: None)
    with pytest.raises(NotesError, match="NOTES_DIR"):
        Vault().capture("anything")


def test_an_edited_file_is_picked_up(vault, tmp_path):
    vault.load()
    fresh = tmp_path / "projects" / "Oracle.md"
    fresh.write_text("Ampere A1, four cores, always free tier.\n")

    assert any(n.title == "Oracle" for n in vault.load())


def test_notes_dir_ignores_a_path_that_is_not_there(monkeypatch, tmp_path):
    class FakeSettings:
        notes_dir = str(tmp_path / "nowhere")

    monkeypatch.setattr(notes_mod, "get_settings", lambda: FakeSettings())
    assert notes_mod.notes_dir() is None


# --- the endpoints the viewer talks to ---


@pytest.fixture
def api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from jarvis import cache
    from jarvis.main import app

    (tmp_path / "Rome.md").write_text("Notes on the [[Punic Wars]].\n")
    (tmp_path / "Punic Wars.md").write_text("Three of them, ending in 146 BC.\n")

    monkeypatch.setattr(notes_mod, "notes_dir", lambda: tmp_path)
    monkeypatch.setattr(notes_mod, "_vault", notes_mod.Vault())
    cache.invalidate("notes_graph")

    with TestClient(app) as client:
        res = client.post("/api/auth/login", json={"password": "test-password", "label": "pytest"})
        client.headers["Authorization"] = f"Bearer {res.json()['token']}"
        yield client
    cache.invalidate("notes_graph")


def test_the_graph_needs_a_device_token(api):
    del api.headers["Authorization"]
    assert api.get("/api/notes/graph").status_code == 401


def test_the_graph_is_node_ids_and_links(api):
    body = api.get("/api/notes/graph").json()
    ids = {n["id"] for n in body["nodes"]}
    assert ids == {0, 1}
    assert body["links"] == [{"source": 0, "target": 1}]


def test_a_note_can_be_fetched_by_its_node_id(api):
    body = api.get("/api/notes/note/0").json()
    assert body["node"] == 0
    assert body["title"] in {"Rome", "Punic Wars"}


def test_a_node_id_outside_the_vault_is_a_404(api):
    assert api.get("/api/notes/note/999").status_code == 404


def test_search_returns_the_same_ids_the_graph_uses(api):
    graph = api.get("/api/notes/graph").json()
    hits = api.get("/api/notes/search", params={"q": "punic wars"}).json()["notes"]

    assert hits
    for hit in hits:
        assert graph["nodes"][hit["node"]]["label"] == hit["title"]


def test_an_unconfigured_vault_names_the_setting(api, monkeypatch):
    from jarvis import cache

    cache.invalidate("notes_graph")
    monkeypatch.setattr(notes_mod, "notes_dir", lambda: None)
    res = api.get("/api/notes/graph")

    assert res.status_code == 404
    assert "NOTES_DIR" in res.json()["detail"]


# --- link building, which is where the shortcuts live ---


def test_a_short_title_links_by_wikilink_but_not_by_mention(tmp_path, monkeypatch):
    (tmp_path / "Q3.md").write_text("Quarterly numbers.\n")
    (tmp_path / "Deliberate.md").write_text("See [[Q3]] for the numbers.\n")
    (tmp_path / "Incidental.md").write_text("The Q3 figures were fine.\n")
    monkeypatch.setattr(notes_mod, "notes_dir", lambda: tmp_path)

    graph = Vault().graph()
    by_label = {n["label"]: n["id"] for n in graph["nodes"]}
    edges = {tuple(sorted((link["source"], link["target"]))) for link in graph["links"]}

    assert tuple(sorted((by_label["Deliberate"], by_label["Q3"]))) in edges
    assert tuple(sorted((by_label["Incidental"], by_label["Q3"]))) not in edges


def test_a_multi_word_title_is_matched_whole(tmp_path, monkeypatch):
    # The first-word index is only a filter; "Punic" alone must not be enough.
    (tmp_path / "Punic Wars.md").write_text("Three of them.\n")
    (tmp_path / "Near.md").write_text("Something about the Punic Wars entirely.\n")
    (tmp_path / "Far.md").write_text("The word Punic appears, and wars do not.\n")
    monkeypatch.setattr(notes_mod, "notes_dir", lambda: tmp_path)

    graph = Vault().graph()
    by_label = {n["label"]: n["id"] for n in graph["nodes"]}
    edges = {tuple(sorted((link["source"], link["target"]))) for link in graph["links"]}

    assert tuple(sorted((by_label["Near"], by_label["Punic Wars"]))) in edges
    assert tuple(sorted((by_label["Far"], by_label["Punic Wars"]))) not in edges


def test_a_wikilink_to_a_note_that_does_not_exist_is_dropped(tmp_path, monkeypatch):
    (tmp_path / "Lonely.md").write_text("Points at [[Nothing At All]].\n")
    monkeypatch.setattr(notes_mod, "notes_dir", lambda: tmp_path)

    assert Vault().graph()["links"] == []


def test_a_large_vault_builds_its_graph_quickly(tmp_path, monkeypatch):
    # The quadratic version of this took most of a minute at this size, which is
    # long enough that the request times out and the galaxy never opens.
    import time

    for i in range(1200):
        (tmp_path / f"Note {i:04d}.md").write_text(
            f"Body text about topic {i % 40}, referring to [[Note {(i + 1) % 1200:04d}]].\n"
        )
    monkeypatch.setattr(notes_mod, "notes_dir", lambda: tmp_path)

    started = time.monotonic()
    graph = Vault().graph()
    elapsed = time.monotonic() - started

    assert len(graph["nodes"]) == 1200
    assert graph["links"]
    assert elapsed < 5, f"graph took {elapsed:.1f}s"
