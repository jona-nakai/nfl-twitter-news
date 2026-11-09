import pytest

from nflnews.classifier import Decision, Usage
from nflnews.evals import Case, Run


def run(
    expect,
    categories=(),
    *,
    newsworthy=True,
    marked_dup=None,
    got_label=False,
    **case,
):
    decision = Decision(
        newsworthy=newsworthy,
        duplicate_of=marked_dup,
        breaking_label=got_label,
        label_text="BREAKING" if got_label else None,
        categories=list(categories),
        headline="h",
        reason="r",
    )
    return Run(Case(id="c", author="a", text="t", expect=expect, **case), decision, [])


def test_grading_by_routed_channels():
    assert run(["breaking", "transactions"], ["transactions"], got_label=True).mistake is None
    assert run([], newsworthy=False).mistake is None
    assert run([], ["transactions"], marked_dup=7, duplicate_of=7).mistake is None
    assert run(["transactions"], ["transactions"], got_label=True).mistake == "false breaking"
    assert run(["breaking", "injuries"], ["injuries"]).mistake == "missed breaking"
    assert run(["injuries"], ["transactions"]).mistake == "wrong topic channel"
    assert run(["injuries"], newsworthy=False).mistake == "dropped news"
    assert run(["injuries"], ["injuries"], marked_dup=3).mistake == "wrongly marked duplicate"
    assert run([], ["rumors"]).mistake == "posted non-news"
    assert run([], ["transactions"], duplicate_of=7).mistake == "posted a duplicate"


def test_also_ok_alternatives():
    r = run(["breaking"], ["transactions"], got_label=True, also_ok=[["breaking", "transactions"]])
    assert r.mistake is None


def test_run_cost_sums_calls_and_is_unknown_for_unpriced_models():
    case = Case(id="c", author="a", text="t", expect=[])
    calls = [Usage("gpt-6-luna", input_tokens=10**6), Usage("gpt-6-luna", output_tokens=10**6)]
    assert Run(case, None, calls).cost == pytest.approx(0.60)
    assert Run(case, None, [Usage("mystery-model", input_tokens=5)]).cost is None


def test_label_graded_on_duplicates_when_the_case_cares():
    # A labeled duplicate upgrades the earlier story, so the label matters even when the post
    # itself isn't posted.
    dup = dict(marked_dup=7, duplicate_of=7)
    assert run([], ["transactions"], got_label=True, label=True, **dup).mistake is None
    assert run([], ["transactions"], label=True, **dup).mistake == "missed breaking label"
    assert (
        run([], ["transactions"], got_label=True, label=False, **dup).mistake
        == "false breaking label"
    )
    assert run([], ["transactions"], got_label=True, **dup).mistake is None
