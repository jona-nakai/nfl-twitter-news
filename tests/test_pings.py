from itertools import combinations, permutations

from nflnews.config import CHANNEL_KEYS
from nflnews.pings import ALL_COMBOS, combo_key, ordered, ping_plan


def pinged_in(plan, subscriptions: frozenset[str]) -> list[str]:
    """Channels in which a member with these subscription roles (and the matching combo role)
    would be mentioned."""
    hits = []
    for key, ping in plan:
        if ping.base_role in subscriptions or subscriptions in ping.combos:
            hits.append(key)
    return hits


def test_one_combo_role_per_subscription_set():
    count = 2 ** len(CHANNEL_KEYS) - 1
    assert len(ALL_COMBOS) == count
    assert len({combo_key(c) for c in ALL_COMBOS}) == count


def test_every_member_pinged_once_in_first_subscribed_channel():
    stories = [
        list(s) for n in range(1, len(CHANNEL_KEYS) + 1) for s in combinations(CHANNEL_KEYS, n)
    ]
    for channels in stories:
        plan = ping_plan(channels)
        for subs in [frozenset()] + list(ALL_COMBOS):
            expected = [k for k in channels if k in subs][:1]
            assert pinged_in(plan, subs) == expected, (channels, sorted(subs))


def test_first_channel_uses_plain_subscription_role():
    plan = ping_plan(["breaking", "injuries"])
    assert plan[0][1].base_role == "breaking"
    assert plan[1][1].base_role is None
    assert all("breaking" not in c and "injuries" in c for c in plan[1][1].combos)


def test_ordered_follows_channel_order():
    for perm in permutations(CHANNEL_KEYS):
        assert ordered(perm) == list(CHANNEL_KEYS)


def test_later_channel_skips_members_already_pinged_for_the_story():
    plan = ping_plan(["breaking"], already=("transactions",))
    for subs in ALL_COMBOS:
        expected = ["breaking"] if "breaking" in subs and "transactions" not in subs else []
        assert pinged_in(plan, subs) == expected, sorted(subs)
