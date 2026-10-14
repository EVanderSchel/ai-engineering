"""The baseline model on a tiny synthetic problem (seconds, no data files)."""

import baseline

MISSIONS = {
    "X": ["worship and bible study church ministry", "christian church congregation worship", "parish ministry prayer"],
    "N": ["youth soccer league games", "little league baseball teams", "soccer club tournaments and leagues"],
    "A": ["art museum exhibitions", "community theater performances", "museum of local history and art"],
}


def train_toy(features="word"):
    x = [m for missions in MISSIONS.values() for m in missions]
    y = [g for g, missions in MISSIONS.items() for _ in missions]
    # min_df=2 in the vectorizers needs repeated words, which the toy data has.
    return baseline.make_model(features, C=10.0, class_weight=None).fit(x, y)


def test_learns_obvious_words_and_ranks_every_group():
    model = train_toy()
    ranked = baseline.ranked_predictions(model, ["sunday church worship", "regional soccer league"])
    assert [r[0] for r in ranked] == ["X", "N"]
    assert sorted(ranked[0]) == ["A", "N", "X"]  # a full ranking, so top-3 is defined


def test_character_features_cope_with_misspellings():
    model = train_toy("word+char")
    assert baseline.ranked_predictions(model, ["MUSEUMM EXHIBITIONZ"])[0][0] == "A"


def test_latency_is_measured_per_single_mission():
    assert baseline.latency_ms(train_toy(), ["church"] * 5) > 0
