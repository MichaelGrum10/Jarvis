"""The markdown vault: indexing, ranking, the graph, and capture.

The case that matters most here is the node index. Search returns positions into
the graph's node array so the viewer can fly the camera to a note without a
second lookup, and if those two ever disagree the dive lands on an unrelated
star — a wrong answer that looks exactly like a right one.
"""

from __future__ import annotations

from pathlib import Path

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


# --- ownership, which is what keeps Syncthing working ---


def test_a_capture_inherits_the_vault_ownership(vault, tmp_path, monkeypatch):
    """Jarvis runs as root in the container; the vault belongs to the user
    Syncthing runs as. Syncthing applies a change by writing a temp file into
    the directory and renaming, so a root-owned captures/ cannot be written to
    at all — the folder silently stops syncing, with no error to see."""
    calls = []
    monkeypatch.setattr(notes_mod.os, "chown", lambda p, u, g: calls.append((str(p), u, g)))
    # Pretend to be somebody other than the vault's owner, which is exactly the
    # container's situation.
    monkeypatch.setattr(notes_mod.os, "geteuid", lambda: 999_999)

    want = tmp_path.stat()
    note = vault.capture("Remember that the box reboots on Sundays")

    owned = {c[0] for c in calls}
    assert str(tmp_path / "captures") in owned, "the directory is the half that blocks Syncthing"
    assert any(note.title in path for path in owned), "and the note file itself"
    assert all((u, g) == (want.st_uid, want.st_gid) for _, u, g in calls)


def test_ownership_is_not_touched_when_we_already_own_the_vault(vault, monkeypatch):
    """Running as the owner — a normal dev machine — must not call chown at all."""
    calls = []
    monkeypatch.setattr(notes_mod.os, "chown", lambda *a: calls.append(a))
    vault.capture("Anything at all")
    assert calls == []


def test_a_capture_still_succeeds_when_chown_is_refused(vault, monkeypatch):
    """Not running as root is the normal case outside the container. The note
    is what matters; its ownership is a bonus."""
    def refuse(*_a):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(notes_mod.os, "chown", refuse)
    monkeypatch.setattr(notes_mod.os, "geteuid", lambda: 0)

    note = vault.capture("Written despite the chown failing")
    assert note.title
    assert vault.search("chown failing")


# --- asking the vault ---


@pytest.fixture
def stub_llm(monkeypatch):
    """Replace the endpoint pool with something that records what it was sent."""
    from jarvis.api import notes as api_notes

    seen = {}

    class Reply:
        content = "Rome fought three Punic Wars, per your Punic Wars note."

    class Fake:
        async def complete(self, messages, **kwargs):
            seen["messages"] = messages
            seen["kwargs"] = kwargs
            return Reply()

    monkeypatch.setattr(api_notes, "get_llm", lambda: Fake())
    api_notes._SESSIONS.clear()
    return seen


def test_asking_puts_the_matching_notes_in_the_system_prompt(api, stub_llm):
    body = api.post("/api/notes/ask", json={"question": "tell me about the punic wars"}).json()

    system = stub_llm["messages"][0]
    assert system["role"] == "system"
    assert "146 BC" in system["content"], "the matching note's text must be in front of the model"
    # Case-insensitive: the rule has to be present, but the prompt gets
    # reworded and a test that pins its capitalisation is testing prose.
    assert "only from the notes" in system["content"].lower()
    assert body["answer"].startswith("Rome fought three")


def test_the_cited_nodes_are_graph_indexes(api, stub_llm):
    graph = api.get("/api/notes/graph").json()
    body = api.post("/api/notes/ask", json={"question": "punic wars"}).json()

    assert body["nodes"]
    for node in body["nodes"]:
        # This is the whole reason the generation fingerprint exists: a cited
        # id that doesn't index the graph sends the camera to the wrong star.
        assert graph["nodes"][node]["label"] in {"Punic Wars", "Rome"}


def test_a_question_matching_nothing_still_answers(api, stub_llm):
    body = api.post("/api/notes/ask", json={"question": "xylophone manufacturing"}).json()

    assert body["nodes"] == []
    assert "No notes matched" in stub_llm["messages"][0]["content"]


def test_follow_ups_carry_the_previous_exchange(api, stub_llm):
    api.post("/api/notes/ask", json={"question": "punic wars", "session": "s"})
    api.post("/api/notes/ask", json={"question": "and how many?", "session": "s"})

    roles = [m["role"] for m in stub_llm["messages"]]
    assert roles == ["system", "user", "assistant", "user"]


def test_sessions_do_not_share_history(api, stub_llm):
    api.post("/api/notes/ask", json={"question": "punic wars", "session": "a"})
    api.post("/api/notes/ask", json={"question": "punic wars", "session": "b"})

    assert [m["role"] for m in stub_llm["messages"]] == ["system", "user"]


def test_a_changed_vault_is_reported_as_stale(api, stub_llm):
    fresh = api.post("/api/notes/ask", json={"question": "punic wars"}).json()
    assert fresh["stale"] is False

    stale = api.post(
        "/api/notes/ask",
        json={"question": "punic wars", "generation": "not-the-current-one"},
    ).json()
    assert stale["stale"] is True
    assert stale["generation"] == fresh["generation"]


def test_asking_needs_a_device_token(api):
    del api.headers["Authorization"]
    assert api.post("/api/notes/ask", json={"question": "hi"}).status_code == 401


def test_an_empty_question_is_refused(api, stub_llm):
    assert api.post("/api/notes/ask", json={"question": "   "}).status_code == 400


def test_a_pool_failure_surfaces_the_pool_s_own_message(api, monkeypatch):
    from jarvis.api import notes as api_notes
    from jarvis.llm.client import LLMError

    class Broken:
        async def complete(self, *a, **k):
            raise LLMError("No endpoint could answer (tried 2, 1 cooling: groq)")

    monkeypatch.setattr(api_notes, "get_llm", lambda: Broken())
    res = api.post("/api/notes/ask", json={"question": "punic wars"})

    assert res.status_code == 503
    # The pool names which endpoints it tried and why each declined; that is far
    # more actionable than "the model failed".
    assert "1 cooling" in res.json()["detail"]


# --- small talk must not drag the camera around ---


def test_small_talk_cites_nothing(api, stub_llm):
    """A greeting is not a research question. If it cited a note, the viewer
    would fly the camera somewhere for no reason at all."""
    for chatter in ("hello", "how are you", "tell me a joke", "thanks"):
        body = api.post("/api/notes/ask", json={"question": chatter}).json()
        assert body["nodes"] == [], f"{chatter!r} should cite nothing"


def test_a_single_incidental_word_is_not_a_citation(api, stub_llm):
    """One shared body word is noise, not relevance — the exact case that made
    'good morning' fly to any note containing the word."""
    body = api.post("/api/notes/ask", json={"question": "three"}).json()
    assert body["nodes"] == []


def test_a_real_notes_question_still_cites(api, stub_llm):
    body = api.post("/api/notes/ask", json={"question": "tell me about the punic wars"}).json()
    assert body["nodes"], "a title match must still clear the floor"


def test_the_answer_is_never_the_note_recited(api, stub_llm):
    """The system prompt has to forbid it explicitly: the note is already on
    screen, and reading it back is the failure mode this endpoint invites."""
    api.post("/api/notes/ask", json={"question": "punic wars"})
    system = stub_llm["messages"][0]["content"]
    assert "Never recite the note back" in system
    assert "sir" in system


# --- the boot greeting's note count ---


def test_summary_counts_notes_and_folders(api):
    body = api.get("/api/notes/summary").json()
    assert body["configured"] is True
    assert body["notes"] == 2
    assert body["folders"] >= 1


def test_summary_needs_a_device_token(api):
    del api.headers["Authorization"]
    assert api.get("/api/notes/summary").status_code == 401


def test_summary_is_calm_about_no_vault(api, monkeypatch):
    monkeypatch.setattr(notes_mod, "notes_dir", lambda: None)
    body = api.get("/api/notes/summary").json()
    assert body == {"notes": 0, "folders": 0, "configured": False}


# --- capture durability, which Syncthing depends on ---


def test_captures_are_written_atomically(vault, tmp_path, monkeypatch):
    """This folder is watched by Syncthing. A plain write is observable
    half-finished, and a truncated note propagating to every other device looks
    like data loss rather than a race. So the file must appear complete or not
    at all — which means a rename, never a write in place."""
    seen = []
    real_replace = notes_mod.os.replace

    def watched(src, dst):
        # At the moment of the rename the destination must not yet exist, and
        # the source must already hold the whole note.
        seen.append((Path(src).name, Path(dst).name, Path(src).read_text()))
        return real_replace(src, dst)

    monkeypatch.setattr(notes_mod.os, "replace", watched)
    note = vault.capture("Remember that the Oracle box reboots on Sundays")

    assert seen, "no rename happened — the write was not atomic"
    src, dst, content = seen[0]
    assert src.startswith(".") and src.endswith(".tmp"), f"temp file was {src!r}"
    assert dst == f"{note.title}.md"
    assert "reboots on Sundays" in content, "the temp file must be complete before the rename"


def test_the_temp_file_never_survives(vault, tmp_path):
    vault.capture("Something worth keeping")
    leftovers = list((tmp_path / "captures").glob(".*"))
    assert leftovers == [], f"temp files left behind: {leftovers}"


def test_repeated_captures_count_upwards_and_never_overwrite(vault, tmp_path):
    """The old code stamped the time and tried once, so two captures in the
    same second overwrote each other."""
    made = [vault.capture("Standing note about Fridays") for _ in range(4)]
    titles = [n.title for n in made]

    assert len(set(titles)) == 4, f"names collided: {titles}"
    for note in made:
        assert (tmp_path / "captures" / f"{note.title}.md").exists()
    # And every one still holds its own content rather than the last writer's.
    assert len(list((tmp_path / "captures").glob("*.md"))) == 4


def test_a_title_that_is_illegal_as_a_filename_is_made_safe(vault, tmp_path):
    note = vault.capture("Ratio: profit / loss <target> for Q3")
    assert "/" not in note.title and ":" not in note.title
    assert not note.title.startswith(".")
    assert (tmp_path / "captures" / f"{note.title}.md").exists()


def test_summary_carries_the_generation_for_polling(api):
    """The open galaxy polls this to notice a note that arrived from the phone,
    so it has to agree with the graph's own fingerprint or every poll would
    look like a change."""
    summary = api.get("/api/notes/summary").json()
    graph = api.get("/api/notes/graph").json()
    assert summary["generation"] == graph["generation"]


def test_the_generation_moves_when_a_note_is_added(api, tmp_path):
    before = api.get("/api/notes/summary").json()["generation"]
    (tmp_path / "Carthage.md").write_text("Delenda est.\n")
    after = api.get("/api/notes/summary").json()["generation"]
    assert before != after, "a new note must change the fingerprint or the watcher never fires"
