"""Approach 1, the classic baseline: TF-IDF features and logistic regression.

    python src/baseline.py            # tune on dev, report dev
    python src/baseline.py --final    # also score the held-out test split once, with the chosen settings

Plain language: count which words (and word fragments) each mission uses, weighting words that are rare
across missions more heavily (TF-IDF), and learn one weight per word per group (logistic regression). It
can't understand meaning, but words like "church", "museum" or "soccer" carry a lot of signal, and it
trains in seconds on a laptop CPU.

Settings are chosen on the dev split from a small grid (feature type, regularization strength, whether rare
groups get extra weight), by macro-F1. Choosing on dev makes the dev score slightly optimistic; the
held-out test score at the end is the honest one.
"""

import argparse
import itertools
import statistics
import time

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import FeatureUnion, make_pipeline

import data
import evaluate

APPROACH = "baseline"
GRID = {
    "features": ("word", "word+char"),
    "C": (1.0, 4.0, 16.0),  # inverse regularization: larger = fits the training data more closely
    "class_weight": (None, "balanced"),  # "balanced" weights rare groups up
}


def make_model(features: str, C: float, class_weight: str | None):
    """TF-IDF features into a multinomial logistic regression. Word unigrams and bigrams capture phrases
    ("food pantry", "little league"); character 3-5-grams within words add robustness to typos and
    all-caps text ("PEDIACTRIC")."""
    word = TfidfVectorizer(ngram_range=(1, 2), min_df=2, sublinear_tf=True, strip_accents="unicode")
    vectorizer = word
    if features == "word+char":
        char = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), min_df=3, sublinear_tf=True)
        vectorizer = FeatureUnion([("word", word), ("char", char)])
    return make_pipeline(vectorizer, LogisticRegression(C=C, class_weight=class_weight, max_iter=3000))


def ranked_predictions(model, missions: list[str]) -> list[list[str]]:
    """Every group, best first, for each mission (from the model's probabilities)."""
    probabilities = model.predict_proba(missions)
    return [[model.classes_[i] for i in row.argsort()[::-1]] for row in probabilities]


def latency_ms(model, missions: list[str], repeats: int = 200) -> float:
    """Median time to classify one mission on its own, as a single-request service would."""
    times = []
    for mission in missions[:repeats]:
        start = time.perf_counter()
        model.predict_proba([mission])
        times.append((time.perf_counter() - start) * 1000)
    return statistics.median(times)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--final", action="store_true", help="also score the held-out test split (once)")
    args = parser.parse_args()

    train, dev = data.load("train"), data.load("dev")
    x_train, y_train = [r["mission"] for r in train], [r["group"] for r in train]
    x_dev = [r["mission"] for r in dev]

    print(f"Tuning on dev ({len(train):,} train, {len(dev):,} dev):")
    trials = []
    for features, C, class_weight in itertools.product(*GRID.values()):
        start = time.perf_counter()
        model = make_model(features, C, class_weight).fit(x_train, y_train)
        seconds = time.perf_counter() - start
        metrics = evaluate.score(dev, ranked_predictions(model, x_dev))
        trials.append({"features": features, "C": C, "class_weight": class_weight, "train_seconds": round(seconds, 1),
                       "accuracy": metrics["accuracy"], "macro_f1": metrics["macro_f1"]})  # fmt: skip
        print(f"  {features:9} C={C:<4} weight={class_weight or 'none':8} accuracy {metrics['accuracy']:.1%} "
              f"macro-F1 {metrics['macro_f1']:.3f} ({seconds:.1f} s)")  # fmt: skip

    best = max(trials, key=lambda t: t["macro_f1"])
    settings = {k: best[k] for k in GRID}
    print(f"Chosen by dev macro-F1: {settings}")
    model = make_model(**settings).fit(x_train, y_train)
    n_features = model[-1].coef_.shape[1]  # one learned weight per feature per group
    details = {"settings": settings, "trials": trials, "features": n_features, "train_seconds": best["train_seconds"]}
    # Runs on the CPU that's already there: no API or GPU charges, so cost per 1,000 predictions is ~$0.
    common = {"latency_ms": latency_ms(model, x_dev), "cost_per_1k_usd": 0.0, "details": details}
    notes = f"{settings['features']} C={settings['C']} weight={settings['class_weight']}"

    metrics = evaluate.record(APPROACH, "dev", dev, ranked_predictions(model, x_dev), notes=notes, **common)
    print(f"\nDev: {evaluate.summary(metrics)} | {common['latency_ms']:.2f} ms per mission | {n_features:,} features")
    worst = sorted(metrics["per_group"].items(), key=lambda kv: kv[1]["f1"])[:5]
    print("  weakest groups (F1):", ", ".join(f"{g} {m['f1']:.2f} (n={m['support']})" for g, m in worst))
    print("  top confusions (gold -> predicted):",
          ", ".join(f"{c['gold']}->{c['predicted']} x{c['count']}" for c in metrics["top_confusions"][:8]))  # fmt: skip

    if args.final:
        test = data.load("test", final=True)
        metrics = evaluate.record(APPROACH, "test", test, ranked_predictions(model, [r["mission"] for r in test]),
                                  notes=notes, **common)  # fmt: skip
        print(f"Test (held out): {evaluate.summary(metrics)}")


if __name__ == "__main__":
    main()
