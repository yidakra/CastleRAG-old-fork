"""Browser-free tests for the user-study capture helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from castlerag.study import capture as cap
from castlerag.ui.youtube import YouTubeMirror

REPO = Path(__file__).resolve().parents[2]
CSV = REPO / "data" / "smoke_day1.csv"


@pytest.fixture()
def mirror() -> YouTubeMirror:
    return YouTubeMirror(mapping={("day1", "Bjorn", 12): "VIDBJORN"})


def _export() -> dict:
    cams = [
        {"camera_id": "Bjorn", "day": "day1", "hour": 12, "start_seconds": 1745.0,
         "match_score": 0.61, "is_best": False, "is_context": False},
        {"camera_id": "Luca", "day": "day1", "hour": 12, "start_seconds": 1745.0,
         "match_score": 0.74, "is_best": True, "is_context": False},
        {"camera_id": "Klaus", "day": "day1", "hour": 12, "start_seconds": 1745.0,
         "match_score": 0.0, "is_best": False, "is_context": True},
    ]
    return {
        "turns": [
            {
                "group_id": "g1", "iteration": 1, "is_refinement": False,
                "question": "What brand is the fridge in the kitchen?",
                "refined_query": None,
                "answer": "It is a **Samsung** fridge "
                          "[[cite:m0:Bjorn:12:29]].",
                "claim": {"text": "Samsung", "support": "partial"},
                "reviews": {},
                "moments": [
                    {"moment_id": "g1-m0", "clock_label": "12:29",
                     "place_label": "Kitchen", "cameras": cams},
                ],
            },
            {
                "group_id": "g2", "iteration": 2, "is_refinement": True,
                "question": "Re-examine", "refined_query": "Re-examine",
                "answer": "Still four.",
                "claim": {"text": "Samsung", "support": "supported"},
                "moments": [],
            },
        ]
    }


def test_all_candidates_match_csv():
    tasks = cap.match_questions(
        cap.CANDIDATE_QUESTIONS, cap.load_ground_truth(CSV)
    )
    assert [t.number for t in tasks] == list(range(1, 9))
    truths = {t.question: t.truth for t in tasks}
    assert truths["What brand is the fridge in the kitchen?"] == "Samsung"
    assert truths["When did Cathal want rösti?"] == "Thursday"
    assert tasks[0].choices[0] == "Samsung" and len(tasks[0].choices) == 4


def test_match_questions_is_whitespace_and_case_insensitive():
    rows = [{"Question": "Who won the first round of UNO?", "Answer": "Klaus"}]
    tasks = cap.match_questions(["  who won the first  round of uno "], rows)
    assert tasks[0].truth == "Klaus"


def test_match_questions_reports_missing():
    with pytest.raises(ValueError, match="not a question"):
        cap.match_questions(["not a question"], [])


def test_load_questions_arg(tmp_path: Path):
    assert cap.load_questions_arg(None) == list(cap.CANDIDATE_QUESTIONS)
    f = tmp_path / "q.txt"
    f.write_text("# header\nA?\n\nB?\n")
    assert cap.load_questions_arg(str(f)) == ["A?", "B?"]
    assert cap.load_questions_arg("A?|B?") == ["A?", "B?"]


def test_parse_task_selection():
    assert cap.parse_task_selection(None, 3) == [1, 2, 3]
    assert cap.parse_task_selection("all", 2) == [1, 2]
    assert cap.parse_task_selection("3,1-2,3", 5) == [1, 2, 3]
    with pytest.raises(ValueError):
        cap.parse_task_selection("9", 8)


def test_slug_and_dir_name():
    task = cap.StudyTask(number=5, question="When did Cathal want rösti?", truth="")
    assert task.dir_name == "task05_when-did-cathal-want-rosti"
    assert len(cap.slugify("word " * 30)) <= 40


def test_resolve_verdicts_default_and_plan():
    cams = _export()["turns"][0]["moments"][0]["cameras"]
    assert cap.top_camera(cams) == "Luca"
    assert cap.resolve_verdicts(cams) == {
        "Bjorn": "refine", "Luca": "confirm", "Klaus": "refine"
    }
    plan = {"others": "ignore", "Klaus": "refine"}
    assert cap.resolve_verdicts(cams, plan) == {
        "Bjorn": "ignore", "Luca": "confirm", "Klaus": "refine"
    }


def test_load_verdict_plan(tmp_path: Path):
    p = tmp_path / "plan.json"
    p.write_text(json.dumps({"2": {"best": "ignore"}}))
    assert cap.load_verdict_plan(p) == {2: {"best": "ignore"}}
    p.write_text(json.dumps({"1": {"Luca": "reject"}}))
    with pytest.raises(ValueError):
        cap.load_verdict_plan(p)
    assert cap.load_verdict_plan(None) == {}


def test_dash_id_selectors_sort_keys_and_escape():
    sel = cap.review_button_selector("Bjorn", "confirm")
    assert sel == (
        '[id="{\\"action\\":\\"confirm\\",\\"cam\\":\\"Bjorn\\",'
        '\\"type\\":\\"review-btn\\"}"]'
    )
    assert cap.review_just_selector("Luca") == (
        '[id="{\\"cam\\":\\"Luca\\",\\"type\\":\\"review-just\\"}"]'
    )
    with pytest.raises(ValueError):
        cap.review_button_selector("Bjorn", "reject")


def test_is_live_badge():
    assert cap.is_live_badge("live RAG")
    assert cap.is_live_badge("  LIVE  rag ")
    assert not cap.is_live_badge("offline")
    assert not cap.is_live_badge(None)


@pytest.mark.parametrize(
    "answer,truth,expected",
    [
        ("It is a **Samsung** fridge [[cite:m0:Bjorn:12:29]].", "Samsung", True),
        ("Four oranges went in.", "4", True),
        ("They used 4 oranges.", "4", True),
        ("They used 14 oranges.", "4", False),
        ("He wanted rosti on thursday.", "Thursday", True),
        ("The logo is W3C's.", "W3C", True),
        ("It was risotto-ish", "Risotto", True),
        ("Nothing relevant.", "Klaus", False),
        ("anything", "", False),
    ],
)
def test_answer_mentions_truth(answer: str, truth: str, expected: bool):
    assert cap.answer_mentions_truth(answer, truth) is expected


def test_plain_answer_text_renders_citations():
    text = cap.plain_answer_text("A **b** [[cite:m0:Luca:12:29]] [x](ref)")
    assert text == "A b Luca 12:29 x"


def test_build_task_entry(mirror: YouTubeMirror):
    task = cap.StudyTask(1, "What brand is the fridge in the kitchen?", "Samsung")
    entry = cap.build_task_entry(
        task, _export(), mirror, mode="live", task_dir=task.dir_name,
        screenshots=["01_answer.png"], verdicts={"Luca": "confirm"},
    )
    assert entry["answer_text"] == "It is a Samsung fridge Bjorn 12:29."
    assert entry["claim"] == "Samsung" and entry["support"] == "partial"
    assert entry["answer_mentions_truth"] is True
    assert entry["answer_mentions_truth_needs_manual_check"] is True
    assert entry["offline_fabricated"] is False
    cited = entry["cited_moments"]
    assert cited[0]["camera_id"] == "Bjorn" and cited[0]["clock_label"] == "12:29"
    assert cited[0]["watch_url"] == "https://www.youtube.com/watch?v=VIDBJORN&t=1745s"
    urls = {c["camera_id"]: c["watch_url"] for c in entry["displayed_cameras"]}
    # Unmapped triples resolve to the placeholder video -> no URL, never invented.
    assert urls == {
        "Bjorn": "https://www.youtube.com/watch?v=VIDBJORN&t=1745s",
        "Luca": None,
        "Klaus": None,
    }
    assert entry["refined"]["support"] == "supported"
    assert entry["refined_mentions_truth"] is False


def test_build_manifest_flags_offline():
    m = cap.build_manifest([], ui_url="u", mode="offline", created="t")
    assert "FABRICATED" in m["note"]
    assert "FABRICATED" not in cap.build_manifest(
        [], ui_url="u", mode="live", created="t"
    )["note"]


def test_merge_entries():
    merged = cap.merge_entries(
        [{"task": 1, "v": "old"}, {"task": 3, "v": "old"}],
        [{"task": 1, "v": "new"}, {"task": 2, "v": "new"}],
    )
    assert [(e["task"], e["v"]) for e in merged] == [
        (1, "new"), (2, "new"), (3, "old")
    ]


def test_callout_layout_scales_and_clamps():
    targets = [
        (1, "a", (10.0, 20.0, 100.0, 50.0)),
        (2, "b", None),
        (3, "c", (5000.0, 5000.0, 10.0, 10.0)),  # off-image
        (4, "d", (700.0, 450.0, 200.0, 200.0)),  # runs off bottom-right
    ]
    out = cap.callout_layout(targets, (1600, 1000), scale=2.0, pad=5, badge_radius=10)
    assert [c.number for c in out] == [1, 4]
    first = out[0]
    assert first.rect == (10, 30, 230, 150)
    # No room left of or above box 1, so its badge falls back inside.
    assert first.badge_center == (30, 50) and first.badge_radius == 20
    last = out[1]
    # box 4 has room on the left: badge centred left of the corner.
    assert last.badge_center == (1390 - 20 - 4, 890 + 20)
    assert last.rect[2] == 1599 and last.rect[3] == 999


def test_annotate_image(tmp_path: Path):
    from PIL import Image

    src = tmp_path / "in.png"
    Image.new("RGB", (200, 100), "white").save(src)
    callouts = cap.callout_layout([(1, "a", (20, 20, 80, 40))], (200, 100))
    cap.annotate_image(src, tmp_path / "out.png", callouts)
    with Image.open(tmp_path / "out.png") as im:
        assert im.size == (200, 100)
        assert im.getpixel((15, 63)) != (255, 255, 255)  # outline drawn


def test_layout_ids_exist_in_dashboard():
    pytest.importorskip("dash")
    from castlerag.ui.layout import build_layout

    ids: set = set()

    def walk(node: object) -> None:
        cid = getattr(node, "id", None)
        if isinstance(cid, str):
            ids.add(cid)
        children = getattr(node, "children", None)
        if isinstance(children, (list, tuple)):
            for child in children:
                walk(child)
        elif children is not None and not isinstance(children, str):
            walk(children)

    walk(build_layout(YouTubeMirror(), mode="live"))
    missing = [i for i in cap.LAYOUT_IDS if i not in ids]
    assert not missing, missing


def test_mode_badge_text_matches_live_check():
    pytest.importorskip("dash")
    from castlerag.ui.layout import _top_bar

    def find(node: object):
        if getattr(node, "id", None) == "mode-badge":
            return node
        children = getattr(node, "children", None)
        for child in children if isinstance(children, list) else [children]:
            if child is not None and not isinstance(child, str):
                hit = find(child)
                if hit is not None:
                    return hit
        return None

    assert cap.is_live_badge(find(_top_bar("live")).children)
    assert not cap.is_live_badge(find(_top_bar("offline")).children)
