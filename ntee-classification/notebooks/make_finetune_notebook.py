"""Writes notebooks/finetune_lora.ipynb (run: python notebooks/make_finetune_notebook.py).

The notebook is generated from this file so its code lives in reviewable Python strings, diffs cleanly, and can
be syntax-checked by the tests (tests/test_notebook.py). Edit here, then regenerate.
"""

import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent

CELLS = [
    (
        "markdown",
        """\
# Step 4: LoRA fine-tuning for NTEE classification

Fine-tunes a small open model (Qwen2.5, 0.5B or 1.5B parameters) to classify nonprofit mission statements into
the 25 NTEE major groups, on Colab's free T4 GPU. Part of the `ntee-classification` project in
https://github.com/EVanderSchel/ai-engineering.

**How it works.** The model reads a mission and a small classification head turns its last hidden state into a
score for each group (one forward pass; the scores rank all 25 groups, so top-3 is defined). With **LoRA**, the
model's own weights stay frozen and only small adapter matrices in its attention layers, plus the new head, are
trained: about 1% of the parameters, which is what fits on a free GPU.

**Rules.** Trains on the train split, picks the best epoch by dev macro-F1, and touches the held-out test split
only when `FINAL = True`. Saves the adapter and a predictions file (the repo scorer's format) to Google Drive.

**Run:** Runtime > Change runtime type > T4 GPU, then Runtime > Run all. About 10 minutes for 0.5B.""",
    ),
    (
        "code",
        """\
# 1. Settings: the only cell you normally edit.
MODEL_NAME = "Qwen/Qwen2.5-0.5B"   # or "Qwen/Qwen2.5-1.5B" (about 3x slower, maybe more accurate)
TRAIN_FRACTION = 1.0               # step 5's data-size curve: e.g. 0.1, 0.25, 0.5 of the training missions
EPOCHS = 3
LEARNING_RATE = 2e-4               # LoRA tolerates higher rates than full fine-tuning
LORA_RANK = 16                     # size of the adapter matrices
BATCH_SIZE = 32                    # missions per step on the GPU; lower it if CUDA runs out of memory (1.5B: 8)
GRADIENT_ACCUMULATION = 1          # update weights every N batches: BATCH_SIZE 8 x 4 behaves like a batch of 32
MAX_TOKENS = 128                   # missions are short: the median is about 33 tokens
SEED = 2026
FINAL = False                      # True only for the one held-out test score at the end
GPU_USD_PER_HOUR = 0.35            # an on-demand T4's price, to express cost per 1,000 predictions
DATASET_URL = "https://raw.githubusercontent.com/EVanderSchel/ai-engineering/main/ntee-classification/data/dataset.csv\"""",
    ),
    (
        "code",
        """\
# 2. Check the GPU and the libraries (Colab preinstalls transformers, datasets, accelerate; PEFT adds LoRA).
!nvidia-smi --query-gpu=name,memory.total --format=csv
!pip uninstall -y -q torchao   # Colab's torchao 0.10 is too old for current PEFT, and LoRA doesn't need it
!pip install -q peft
import torch, transformers, peft, datasets
print("torch", torch.__version__, "| transformers", transformers.__version__, "| peft", peft.__version__,
      "| datasets", datasets.__version__, "| CUDA:", torch.cuda.is_available())
assert torch.cuda.is_available(), "No GPU: Runtime > Change runtime type > T4 GPU"
""",
    ),
    (
        "code",
        """\
# 3. Google Drive, where the adapter and predictions are saved (they survive the Colab session ending).
from google.colab import drive
drive.mount("/content/drive")
import pathlib, re
RUN_NAME = re.sub(r"[^a-z0-9.]+", "-", MODEL_NAME.split("/")[-1].lower()) + (f"-frac{TRAIN_FRACTION}" if TRAIN_FRACTION < 1 else "")
OUT_DIR = pathlib.Path("/content/drive/MyDrive/ntee-classification") / RUN_NAME
OUT_DIR.mkdir(parents=True, exist_ok=True)
print("saving to", OUT_DIR)
""",
    ),
    (
        "code",
        """\
# 4. The dataset from GitHub: 12,078 missions with their NTEE group and split.
import pandas as pd
df = pd.read_csv(DATASET_URL, dtype=str, keep_default_na=False)
GROUPS = sorted(df["group"].unique())
assert len(GROUPS) == 25, GROUPS
label_of = {g: i for i, g in enumerate(GROUPS)}
train_df = df[df["split"] == "train"]
if TRAIN_FRACTION < 1:  # a stratified subsample, for the data-size curve
    train_df = train_df.groupby("group", group_keys=False).apply(
        lambda g: g.sample(max(1, round(len(g) * TRAIN_FRACTION)), random_state=SEED))
dev_df = df[df["split"] == "dev"]
test_df = df[df["split"] == "test"] if FINAL else None  # held out unless FINAL
print(f"train {len(train_df):,} | dev {len(dev_df):,} | test {'held out' if test_df is None else len(test_df)}")
""",
    ),
    (
        "code",
        """\
# 5. The model: Qwen2.5 with a 25-way classification head, and LoRA adapters on the attention layers.
from transformers import AutoTokenizer, AutoModelForSequenceClassification
from peft import LoraConfig, TaskType, get_peft_model
torch.manual_seed(SEED)
tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
if tokenizer.pad_token is None:
    tokenizer.pad_token = tokenizer.eos_token
model = AutoModelForSequenceClassification.from_pretrained(
    MODEL_NAME, num_labels=len(GROUPS), id2label=dict(enumerate(GROUPS)), label2id=label_of,
    dtype=torch.float32)  # T4s have no bfloat16; train in fp32 weights with fp16 mixed precision
model.config.pad_token_id = tokenizer.pad_token_id
lora = LoraConfig(task_type=TaskType.SEQ_CLS, r=LORA_RANK, lora_alpha=2 * LORA_RANK, lora_dropout=0.05,
                  target_modules=["q_proj", "k_proj", "v_proj", "o_proj"])
model = get_peft_model(model, lora)  # SEQ_CLS also keeps the new head ("score") trainable
model.print_trainable_parameters()
""",
    ),
    (
        "code",
        """\
# 6. Tokenize.
from datasets import Dataset
def encode(frame):
    ds = Dataset.from_dict({"text": list(frame["mission"]), "label": [label_of[g] for g in frame["group"]]})
    return ds.map(lambda b: tokenizer(b["text"], truncation=True, max_length=MAX_TOKENS), batched=True,
                  remove_columns=["text"])
train_ds, dev_ds = encode(train_df), encode(dev_df)
""",
    ),
    (
        "code",
        """\
# 7. Train: evaluate on dev after each epoch and keep the epoch with the best macro-F1.
import inspect, math, numpy as np, time
from sklearn.metrics import accuracy_score, f1_score
from transformers import DataCollatorWithPadding, Trainer, TrainingArguments

def compute_metrics(prediction):
    logits, labels = prediction
    predicted = np.argmax(logits, axis=-1)
    return {"accuracy": accuracy_score(labels, predicted), "macro_f1": f1_score(labels, predicted, average="macro")}

total_steps = math.ceil(len(train_ds) / (BATCH_SIZE * GRADIENT_ACCUMULATION)) * EPOCHS
wanted = dict(
    output_dir="/content/checkpoints", num_train_epochs=EPOCHS, learning_rate=LEARNING_RATE,
    per_device_train_batch_size=BATCH_SIZE, gradient_accumulation_steps=GRADIENT_ACCUMULATION,
    per_device_eval_batch_size=64,
    warmup_steps=int(0.06 * total_steps),  # ramp the learning rate up over the first 6% of steps
    weight_decay=0.01, eval_strategy="epoch", save_strategy="epoch", save_total_limit=1,
    load_best_model_at_end=True, metric_for_best_model="macro_f1", greater_is_better=True,
    fp16=True, logging_steps=50, report_to="none", seed=SEED)
# Library versions change fast (transformers 5 removed warmup_ratio): pass only the settings this version
# accepts, and say which it lacks.
accepted = inspect.signature(TrainingArguments.__init__).parameters
skipped = sorted(set(wanted) - set(accepted))
if skipped:
    print("transformers", transformers.__version__, "doesn't accept:", skipped)
args = TrainingArguments(**{k: v for k, v in wanted.items() if k in accepted})
trainer = Trainer(model=model, args=args, train_dataset=train_ds, eval_dataset=dev_ds,
                  data_collator=DataCollatorWithPadding(tokenizer), compute_metrics=compute_metrics)
start = time.time()
trainer.train()
TRAIN_MINUTES = (time.time() - start) / 60
print(f"trained in {TRAIN_MINUTES:.1f} minutes")
print(trainer.evaluate())
""",
    ),
    (
        "code",
        """\
# 8. Predict: a full ranking of the 25 groups per mission, the top 3 probabilities (the model's confidence,
#    for step 5's cascade), plus speed.
import json, statistics
model.eval()

@torch.no_grad()
def rank(missions, batch_size=64):
    ranked, top_probs = [], []
    for i in range(0, len(missions), batch_size):
        batch = tokenizer(missions[i:i + batch_size], truncation=True, max_length=MAX_TOKENS, padding=True,
                          return_tensors="pt").to(model.device)
        with torch.autocast("cuda", dtype=torch.float16):
            logits = model(**batch).logits.float()
        probs = logits.softmax(dim=-1)
        order = probs.argsort(dim=-1, descending=True)
        ranked += [[GROUPS[j] for j in row] for row in order.tolist()]
        top_probs += [[round(p, 4) for p in row] for row in probs.gather(-1, order[:, :3]).tolist()]
    return ranked, top_probs

def single_latency_ms(missions, n=200):
    times = []
    for mission in missions[:n]:
        torch.cuda.synchronize(); start = time.perf_counter()
        rank([mission], batch_size=1)
        torch.cuda.synchronize(); times.append((time.perf_counter() - start) * 1000)
    return statistics.median(times)

def batch_ms_per_mission(missions):
    torch.cuda.synchronize(); start = time.perf_counter()
    rank(missions)
    torch.cuda.synchronize()
    return (time.perf_counter() - start) * 1000 / len(missions)

dev_missions = list(dev_df["mission"])
dev_ranked, dev_probs = rank(dev_missions)
latency = single_latency_ms(dev_missions)
per_mission = batch_ms_per_mission(dev_missions)
accuracy = sum(r[0] == g for r, g in zip(dev_ranked, dev_df["group"])) / len(dev_df)
print(f"dev accuracy {accuracy:.1%} | {latency:.1f} ms for one mission | {per_mission:.2f} ms per mission in batches")
""",
    ),
    (
        "code",
        """\
# 9. Save the adapter and the predictions (to Drive), and download the predictions for the repo's scorer:
#    put the file in ntee-classification/results/finetuned/ and run  python src/finetuned.py <file>
from google.colab import files
model.save_pretrained(OUT_DIR / "adapter")
tokenizer.save_pretrained(OUT_DIR / "adapter")

def predictions_file(split, frame, ranked, probs):
    payload = {
        "approach": "lora_" + RUN_NAME, "split": split, "model": MODEL_NAME,
        "settings": {"train_fraction": TRAIN_FRACTION, "train_examples": len(train_df), "epochs": EPOCHS,
                     "learning_rate": LEARNING_RATE, "lora_rank": LORA_RANK, "batch_size": BATCH_SIZE,
                     "gradient_accumulation": GRADIENT_ACCUMULATION,
                     "max_tokens": MAX_TOKENS, "seed": SEED},
        "trainable_parameters": sum(p.numel() for p in model.parameters() if p.requires_grad),
        "train_minutes": round(TRAIN_MINUTES, 2), "gpu": torch.cuda.get_device_name(0),
        "latency_ms": round(latency, 3), "batch_ms_per_mission": round(per_mission, 4),
        "gpu_usd_per_hour": GPU_USD_PER_HOUR,
        "versions": {"torch": torch.__version__, "transformers": transformers.__version__, "peft": peft.__version__},
        "predictions": [{"ein": e, "ranked": r, "probs": q} for e, r, q in zip(frame["ein"], ranked, probs)],
    }
    path = OUT_DIR / f"lora_{RUN_NAME}_{split}.json"
    path.write_text(json.dumps(payload))
    return path

paths = [predictions_file("dev", dev_df, dev_ranked, dev_probs)]
if FINAL:
    paths.append(predictions_file("test", test_df, *rank(list(test_df["mission"]))))
for path in paths:
    print("saved", path)
    files.download(str(path))
""",
    ),
]


def notebook() -> dict:
    cells = []
    for kind, source in CELLS:
        cell = {"cell_type": kind, "metadata": {}, "source": source.splitlines(keepends=True)}
        if kind == "code":
            cell |= {"execution_count": None, "outputs": []}
        cells.append(cell)
    return {
        "cells": cells,
        "metadata": {
            "accelerator": "GPU",
            "colab": {"provenance": [], "gpuType": "T4"},
            "kernelspec": {"display_name": "Python 3", "name": "python3"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 0,
    }


if __name__ == "__main__":
    path = HERE / "finetune_lora.ipynb"
    path.write_text(json.dumps(notebook(), indent=1) + "\n", encoding="utf-8")
    print("wrote", path)
