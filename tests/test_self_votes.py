"""A seat's vote for its own (anonymous) answer is left out of the places; the ballots themselves are kept."""
import random
import threading

from committee import config as c, engine as e, insights, prompts, report_md


def _ok(*ballots):
    return {who: {"status": "ok", "ranking": list(r)} for who, r in ballots}


def test_every_seat_voting_for_itself_changes_nothing():
    # four seats, each puts itself first and otherwise agrees: D > C > B > A
    reviews = _ok(("A", "ADCB"), ("B", "BDCA"), ("C", "CDBA"), ("D", "DCBA"))
    rows = e.aggregate(reviews, list("ABCD"))
    assert [r["label"] for r in rows] == list("DCBA")
    assert rows[0]["first_votes"] == 3 and rows[0]["avg_rank"] == 1.0 and rows[0]["of"] == 3


def test_a_self_vote_can_no_longer_win_the_vote():
    # A puts itself first; B modestly puts itself last; C and D both prefer B to A.
    reviews = _ok(("A", "ABCD"), ("B", "ACDB"), ("C", "BACD"), ("D", "BACD"))
    with_self = {lab: sum(r["ranking"].index(lab) + 1 for r in reviews.values()) / 4 for lab in "AB"}
    assert with_self["A"] < with_self["B"]                          # counting self-votes, A would win
    rows = e.aggregate(reviews, list("ABCD"))
    assert rows[0]["label"] == "B" and rows[0]["avg_rank"] == 1.0  # without them, every other seat put B first
    a = next(r for r in rows if r["label"] == "A")
    assert round(a["avg_rank"], 3) == 1.667 and a["first_votes"] == 1


def test_ties_share_a_place_and_are_not_a_clear_first():
    reviews = _ok(("A", "BA"), ("B", "AB"))
    rows = e.aggregate(reviews, ["A", "B"])
    assert e.places(rows) == {"A": 1, "B": 1} and not e.clear_first(rows)
    assert e.clear_first(e.aggregate(_ok(("A", "BAC"), ("B", "CBA"), ("C", "BCA")), list("ABC")))


def _run(settings, fake_llm, **kw):
    run = e.new_run(c.build_roster(settings), question="Swap GB-7 now?", rng=random.Random(2))
    llm = fake_llm(**kw)
    while run["next_stage"]:
        e.advance(run, llm, threading.Event(), lambda _m: None)
    run["status"] = "done"
    return run


def test_a_tie_at_the_top_is_not_reported_as_a_winner(settings, fake_llm):
    run = _run(settings, fake_llm)
    run["ranking"] = [{"label": "A", "avg_rank": 1.5, "first_votes": 2, "votes": 3, "of": 3},
                      {"label": "B", "avg_rank": 1.5, "first_votes": 2, "votes": 3, "of": 3}]
    assert not any("ranked first by its peers" in x for x in report_md.exec_summary(run)["stood"])
    assert not any(i["title"] == "Unanimous first place" for i in insights.run_insights(run))
    st = insights.standings([run])
    assert all(r["wins"] == 0 for r in st["table"])


def test_new_runs_say_self_votes_are_left_out_old_runs_do_not(settings, fake_llm):
    run = _run(settings, fake_llm)
    assert e.self_votes_left_out(run)
    _, text = prompts.chairman(run, [], {}, run["ranking"])
    assert "its own answer is not counted" in text
    old = dict(run, ranking=[{k: v for k, v in r.items() if k != "of"} for r in run["ranking"]])
    assert not e.self_votes_left_out(old)
    assert "its own answer is not counted" not in prompts.chairman(old, [], {}, old["ranking"])[1]


def test_unanimous_means_every_other_reviewer(settings, fake_llm):
    run = _run(settings, fake_llm)
    labs = [s["label"] for s in run["seats"]]
    top = labs[0]
    run["reviews"] = {lab: {"status": "ok", "ranking": [top] + [x for x in labs if x != top]} if lab != top
                      else {"status": "ok", "ranking": labs[1:] + [top]} for lab in labs}   # top ranks itself last
    run["ranking"] = e.aggregate(run["reviews"], labs)
    found = [i for i in insights.run_insights(run) if i["title"] == "Unanimous first place"]
    assert found and found[0]["text"].startswith("Every other reviewer ranked")


def test_the_pdf_explains_it(settings, fake_llm, tmp_path):
    import pytest
    fitz = pytest.importorskip("fitz")
    from committee import report_pdf
    run = _run(settings, fake_llm)
    text = " ".join(pg.get_text() for pg in fitz.open(report_pdf.render(run, tmp_path / "r.pdf"))).replace("\n", " ")
    assert "left out of the places" in text
