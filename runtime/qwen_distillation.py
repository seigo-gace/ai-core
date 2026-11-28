"""Offline text-only Qwen3.5-4B LoRA trainer. No service or CLI.

The owning application must inject its authenticated KB reader, which returns
only independently accepted, currently authorized teacher examples. A raw
candidate or caller-supplied rights dict is never an authorization. That
reader is NOT_CONNECTED in the current deployment. Do not call this backend
from a public request. Host resource admission remains the owner's duty.
"""
from __future__ import annotations
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import tempfile

MODEL = "Qwen/Qwen3.5-4B"
TEACHER = "deepseek/deepseek-v4.1-flash-1017"
SCHEMA = "debugai.teacher-training-candidate/v1"
END = "<|im_end|>"
HASH = re.compile(r"[0-9a-f]{64}\Z")
REV = re.compile(r"[0-9a-f]{40}\Z")


class DistillationError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise DistillationError(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def file_digest(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def validate_examples(rows):
    require(isinstance(rows, list) and rows, "DISTILLATION_EMPTY_BATCH")
    seen, result = set(), []
    for row in rows:
        require(isinstance(row, dict) and row.get("schema") == SCHEMA
                and row.get("teacher_model") == TEACHER
                and row.get("student_model") == MODEL
                and row.get("dataset_split") in ("sft", "recovery")
                and isinstance(row.get("case_id"), str) and row["case_id"].strip()
                and row.get("scenario_group") == row["case_id"]
                and row["case_id"] not in seen, "DISTILLATION_CASE_INVALID")
        m = row.get("messages")
        require(isinstance(m, list) and len(m) == 3 and all(
            isinstance(v, dict) and set(v) == {"role", "content"} and v["role"] == role
            and isinstance(v["content"], str) and v["content"].strip()
            for v, role in zip(m, ("system", "user", "assistant"))), "DISTILLATION_MESSAGES_INVALID")
        require(not any(s in v["content"] for v in m for s in
                        ("<|im_start|>", END, "<|vision_start|>", "<|vision_end|>",
                         "<think>", "</think>")), "DISTILLATION_TEMPLATE_CONTROL_INVALID")
        require(row.get("example_digest") == digest(m)
                and row.get("prompt_digest") == digest({"system": m[0]["content"], "user": m[1]["content"]}),
                "DISTILLATION_PROMPT_HASH_MISMATCH")
        try:
            task, answer = json.loads(m[1]["content"]), json.loads(m[2]["content"])
        except (TypeError, ValueError) as exc:
            raise DistillationError("DISTILLATION_JSON_INVALID") from exc
        require(isinstance(task, dict) and set(task) == {"task"} and isinstance(task["task"], dict)
                and task["task"].get("case_id") == row["case_id"]
                and task["task"].get("data_class") == "SYNTHETIC_QUALIFICATION"
                and row.get("input_digest") == digest(task["task"]) and isinstance(answer, dict)
                and row.get("output_digest") == digest(answer), "DISTILLATION_INPUT_OUTPUT_HASH_MISMATCH")
        seen.add(row["case_id"])
        result.append((row["case_id"], row["example_digest"], m))
    return result


def read_verified_batch(reader, version):
    require(callable(reader), "DISTILLATION_TRUSTED_KB_READER_NOT_CONNECTED")
    require(isinstance(version, str) and version.strip(), "DISTILLATION_DATASET_VERSION_INVALID")
    # Owner-composed reader must check actual rights, independent acceptance,
    # split isolation and source/teacher artifact binding. Exceptions propagate.
    return validate_examples(reader(version))


def completion_tokens(tokenizer, messages, max_length):
    """Official non-thinking template, completion-only labels, no truncation."""
    prefix = tokenizer.apply_chat_template(messages[:2], tokenize=False,
                                           add_generation_prompt=True, enable_thinking=False)
    full = tokenizer.apply_chat_template(messages, tokenize=False,
                                         add_generation_prompt=False, enable_thinking=False)
    require(isinstance(prefix, str) and isinstance(full, str)
            and full == prefix + messages[2]["content"].strip() + END + "\n",
            "DISTILLATION_CHAT_TEMPLATE_MISMATCH")
    data = tokenizer(full, add_special_tokens=False, return_offsets_mapping=True)
    ids, offsets = list(data["input_ids"]), list(data["offset_mapping"])
    require(len(ids) == len(offsets) and 1 < len(ids) <= max_length,
            "DISTILLATION_SEQUENCE_BUDGET_EXCEEDED")
    labels = []
    boundary = len(prefix)
    for token, (start, end) in zip(ids, offsets):
        require(0 <= start <= end <= len(full) and not (start < boundary < end),
                "DISTILLATION_TOKEN_BOUNDARY_INVALID")
        labels.append(token if start >= boundary and end > start else -100)
    require(labels[0] == -100 and tokenizer.convert_tokens_to_ids(END) in labels
            and sum(v != -100 for v in labels) > 1, "DISTILLATION_ASSISTANT_MASK_INVALID")
    return ids, labels


def adapter_digest(params):
    import torch
    sha = hashlib.sha256()
    for name, parameter in params:
        sha.update(name.encode("utf-8") + b"\0")
        sha.update(parameter.detach().cpu().contiguous().view(torch.uint8).numpy().tobytes())
    return sha.hexdigest()


def optimize(model, encoded, *, device, epochs, accumulation, seed, learning_rate):
    """Actual optimizer steps; only LoRA weights, token-weighted accumulation."""
    import torch
    params = [(n, p) for n, p in model.named_parameters() if p.requires_grad]
    require(params and all("lora_" in n for n, _ in params), "DISTILLATION_LORA_TARGET_INVALID")
    before = adapter_digest(params)
    optimizer = torch.optim.AdamW([p for _, p in params], lr=learning_rate)
    order, rng = list(range(len(encoded))), random.Random(seed)
    losses, updates, supervised = [], 0, 0
    for _ in range(epochs):
        rng.shuffle(order)
        for start in range(0, len(order), accumulation):
            group = order[start:start + accumulation]
            optimizer.zero_grad(set_to_none=True)
            count = sum(sum(v != -100 for v in encoded[i][3][1:]) for i in group)
            require(count > 0, "DISTILLATION_NO_SUPERVISION")
            for i in group:
                _, _, ids, labels = encoded[i]
                n = sum(v != -100 for v in labels[1:])
                inputs = torch.tensor([ids], dtype=torch.long, device=device)
                targets = torch.tensor([labels], dtype=torch.long, device=device)
                loss = model(input_ids=inputs, attention_mask=torch.ones_like(inputs),
                             labels=targets, use_cache=False).loss
                value = float(loss.detach().cpu())
                require(math.isfinite(value), "DISTILLATION_NONFINITE_LOSS")
                (loss * (n / count)).backward()
                losses.append(value * n)
                supervised += n
            norm = torch.nn.utils.clip_grad_norm_([p for _, p in params], 1.0, error_if_nonfinite=True)
            require(torch.isfinite(norm).item(), "DISTILLATION_NONFINITE_GRADIENT")
            optimizer.step()
            updates += 1
    after = adapter_digest(params)
    require(updates > 0 and before != after and supervised > 0, "DISTILLATION_WEIGHTS_NOT_UPDATED")
    return {"optimizer_steps": updates, "supervised_tokens": supervised,
            "mean_training_loss": sum(losses) / supervised, "adapter_before_sha256": before,
            "adapter_after_sha256": after}


def verify_checkpoint(root, revision, hashes):
    require(isinstance(revision, str) and REV.fullmatch(revision)
            and isinstance(hashes, dict) and hashes and root.is_dir() and not root.is_symlink(),
            "DISTILLATION_CHECKPOINT_IDENTITY_REQUIRED")
    files = {p.name for p in root.iterdir() if p.is_file()}
    require({"config.json", "tokenizer.json", "tokenizer_config.json"} <= files
            and any(n.endswith(".safetensors") for n in files)
            and files == set(hashes) and not any(p.is_symlink() or not p.is_file() for p in root.iterdir()),
            "DISTILLATION_CHECKPOINT_FILES_INVALID")
    for name, expected in hashes.items():
        require(isinstance(expected, str) and HASH.fullmatch(expected)
                and file_digest(root / name) == expected, "DISTILLATION_CHECKPOINT_HASH_MISMATCH")
    cfg = json.loads((root / "config.json").read_text(encoding="utf-8"))
    tc = cfg.get("text_config", {})
    require(cfg.get("model_type") == "qwen3_5" and tc.get("hidden_size") == 2560
            and tc.get("num_hidden_layers") == 32 and tc.get("vocab_size") == 248320,
            "DISTILLATION_NOT_QWEN35_4B")


def run_sft(*, read_authorized_verified_batch, dataset_version, checkpoint_dir,
            checkpoint_revision, checkpoint_sha256, output_dir, device, dtype,
            epochs=1, max_length=2048, accumulation=4, cpu_threads=6,
            seed=42, learning_rate=1e-4, previous_adapter=None, previous_adapter_sha256=None):
    """Offline training only; neither production adoption nor quality approval."""
    rows = read_verified_batch(read_authorized_verified_batch, dataset_version)
    require(device in ("cpu", "cuda") and dtype in ("bfloat16", "float32")
            and type(epochs) is int and 1 <= epochs <= 20
            and type(max_length) is int and 16 <= max_length <= 8192
            and type(accumulation) is int and 1 <= accumulation <= 64
            and type(cpu_threads) is int and 1 <= cpu_threads <= 6
            and type(seed) is int and 0 <= seed < 2**32
            and type(learning_rate) in (int, float) and 0 < learning_rate <= .001,
            "DISTILLATION_CONFIG_INVALID")
    root, target = Path(checkpoint_dir), Path(output_dir)
    require(target.parent.is_dir() and not target.exists() and not target.is_symlink(),
            "DISTILLATION_OUTPUT_PATH_INVALID")
    verify_checkpoint(root, checkpoint_revision, checkpoint_sha256)
    import torch
    from transformers import AutoTokenizer, Qwen3_5ForCausalLM
    from peft import LoraConfig, PeftModel, TaskType, get_peft_model
    require(device != "cuda" or torch.cuda.is_available(), "DISTILLATION_GPU_UNAVAILABLE")
    require(device != "cuda" or dtype != "bfloat16" or torch.cuda.is_bf16_supported(),
            "DISTILLATION_BF16_UNAVAILABLE")
    torch.set_num_threads(cpu_threads)
    torch.manual_seed(seed)
    tokenizer = AutoTokenizer.from_pretrained(root, local_files_only=True, trust_remote_code=False)
    encoded = [(case, sha, *completion_tokens(tokenizer, m, max_length)) for case, sha, m in rows]
    model, info = Qwen3_5ForCausalLM.from_pretrained(root, local_files_only=True,
                    use_safetensors=True, dtype=getattr(torch, dtype), output_loading_info=True)
    require(not info.get("missing_keys") and not info.get("mismatched_keys")
            and not info.get("error_msgs"), "DISTILLATION_BASE_WEIGHTS_INCOMPLETE")
    model.config.use_cache = False
    if previous_adapter is not None:
        prior = Path(previous_adapter)
        require(isinstance(previous_adapter_sha256, dict)
                and set(previous_adapter_sha256) == {"adapter_config.json", "adapter_model.safetensors"}
                and all((prior / f).is_file() and not (prior / f).is_symlink()
                        and isinstance(s, str) and HASH.fullmatch(s)
                        and file_digest(prior / f) == s for f, s in previous_adapter_sha256.items()),
                "DISTILLATION_PRIOR_ADAPTER_HASH_MISMATCH")
        cfg = json.loads((prior / "adapter_config.json").read_text(encoding="utf-8"))
        require(cfg.get("base_model_name_or_path") == MODEL and cfg.get("revision") == checkpoint_revision,
                "DISTILLATION_PRIOR_BASE_MISMATCH")
        model = PeftModel.from_pretrained(model, prior, is_trainable=True)
    else:
        require(previous_adapter_sha256 is None, "DISTILLATION_PRIOR_ADAPTER_INVALID")
        model = get_peft_model(model, LoraConfig(task_type=TaskType.CAUSAL_LM,
                         r=16, lora_alpha=32, lora_dropout=.05, target_modules="all-linear", bias="none"))
    model.to(device)
    model.gradient_checkpointing_enable()
    model.enable_input_require_grads()
    model.train()
    names = [n for n, p in model.named_parameters() if p.requires_grad]
    require(all(any(part in n for n in names) for part in ("q_proj", "in_proj_qkv", "gate_proj")),
            "DISTILLATION_HYBRID_LORA_TARGET_INVALID")
    metrics = optimize(model, encoded, device=device, epochs=epochs, accumulation=accumulation,
                       seed=seed, learning_rate=learning_rate)
    stage = Path(tempfile.mkdtemp(prefix=".distill-", dir=target.parent))
    try:
        for cfg in model.peft_config.values():
            cfg.base_model_name_or_path, cfg.revision = MODEL, checkpoint_revision
        model.save_pretrained(stage, safe_serialization=True)
        artifacts = {name: file_digest(stage / name)
                     for name in ("adapter_config.json", "adapter_model.safetensors")}
        report = {"schema": "gace.qwen-lora-result.v1", "model": MODEL,
                  "checkpoint_revision": checkpoint_revision, "checkpoint_sha256": checkpoint_sha256,
                  "dataset_version": dataset_version,
                  "examples": [{"case_id": case, "example_digest": sha} for case, sha, _, _ in encoded],
                  "device": device, "dtype": dtype, "epochs": epochs, "seed": seed,
                  "max_length": max_length, "accumulation": accumulation, "learning_rate": learning_rate,
                  **metrics, "artifact_sha256": artifacts,
                  "previous_adapter_sha256": previous_adapter_sha256,
                  "independent_quality_status": "NOT_EVALUATED", "production_adopted": False}
        (stage / "training-result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        # Owner serializes immutable run IDs. Do not replace an existing run.
        require(not target.exists(), "DISTILLATION_OUTPUT_ALREADY_EXISTS")
        os.rename(stage, target)
        return report
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
