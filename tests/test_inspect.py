"""Dataset inspection helpers."""

from __future__ import annotations

import json

from app.classifier.dataset import LabeledPrompt, write_dataset
from app.classifier.inspect import load_audit, summarize
from app.tiers.base import TierName


def test_load_audit_returns_empty_when_no_sidecar(tmp_path):
    path = tmp_path / "d.csv"
    write_dataset([LabeledPrompt("hi", TierName.LOCAL)], path)
    assert load_audit(path) == {}


def test_load_audit_reads_records_keyed_by_prompt(tmp_path):
    path = tmp_path / "d.csv"
    write_dataset([LabeledPrompt("hi", TierName.LOCAL)], path)
    audit = path.with_suffix(path.suffix + ".audit.jsonl")
    audit.write_text(
        json.dumps({"prompt": "hi", "label": "local", "verdicts": {}, "answers": {}}) + "\n"
    )
    loaded = load_audit(path)
    assert loaded["hi"]["label"] == "local"


def test_summary_warns_on_a_missing_class(tmp_path, capsys):
    rows = [LabeledPrompt(f"p{i}", TierName.HAIKU) for i in range(10)]
    summarize(rows, {})
    out = capsys.readouterr().out
    assert "not all three tiers appear" in out


def test_summary_reports_the_baseline_a_model_must_beat(capsys):
    rows = [LabeledPrompt(f"p{i}", TierName.HAIKU) for i in range(8)]
    rows += [LabeledPrompt(f"q{i}", TierName.LOCAL) for i in range(2)]
    summarize(rows, {})
    assert "0.800" in capsys.readouterr().out
