import json
import unittest
from pathlib import Path
import tempfile
from runtime.qwen_distillation import (
    DistillationError, MODEL, TEACHER, SCHEMA, digest, file_digest,
    validate_examples, read_verified_batch, completion_tokens, verify_checkpoint, run_sft,
)


def example(case="train-01"):
    task = {"case_id": case, "data_class": "SYNTHETIC_QUALIFICATION"}
    answer = {"decision": "verify"}
    m = [{"role": "system", "content": "Answer"},
         {"role": "user", "content": json.dumps({"task": task})},
         {"role": "assistant", "content": json.dumps(answer)}]
    return {"schema": SCHEMA, "case_id": case, "scenario_group": case,
            "dataset_split": "sft", "teacher_model": TEACHER, "student_model": MODEL,
            "messages": m, "example_digest": digest(m),
            "prompt_digest": digest({"system": m[0]["content"], "user": m[1]["content"]}),
            "input_digest": digest(task), "output_digest": digest(answer),
            "verification_status": "candidate_not_independently_verified",
            "training_execution_allowed": False}


class FakeTokenizer:
    def apply_chat_template(self, messages, *, tokenize, add_generation_prompt, enable_thinking):
        assert tokenize is False and enable_thinking is False
        prefix = "S:" + messages[0]["content"] + "\nU:" + messages[1]["content"] + "\nA:<think>\n\n</think>\n\n"
        return prefix if add_generation_prompt else prefix + messages[2]["content"] + "<|im_end|>\n"

    def __call__(self, text, *, add_special_tokens, return_offsets_mapping):
        assert not add_special_tokens and return_offsets_mapping
        ids, offsets = [], []
        i = 0
        while i < len(text):
            if text.startswith("<|im_end|>", i):
                ids.append(248044)
                offsets.append((i, i + len("<|im_end|>")))
                i += len("<|im_end|>")
            else:
                ids.append(ord(text[i]))
                offsets.append((i, i + 1))
                i += 1
        return {"input_ids": ids, "offset_mapping": offsets}

    def convert_tokens_to_ids(self, text):
        assert text == "<|im_end|>"
        return 248044


class DistillationTests(unittest.TestCase):
    def test_candidate_shape_not_permission(self):
        row = example()
        self.assertEqual(validate_examples([row])[0][0], "train-01")
        with self.assertRaisesRegex(DistillationError, "TRUSTED_KB_READER_NOT_CONNECTED"):
            read_verified_batch(None, "v0.3")
        with self.assertRaisesRegex(DistillationError, "TRUSTED_KB_READER_NOT_CONNECTED"):
            run_sft(read_authorized_verified_batch=None, dataset_version="v0.3",
                    checkpoint_dir="not-read", checkpoint_revision="",
                    checkpoint_sha256={}, output_dir="not-created", device="cpu", dtype="bfloat16")

    def test_reader_failure_not_replaced_by_candidate_flag(self):
        def denied(_):
            raise DistillationError("LEARNING_RIGHTS_CONFIRMATION_NOT_CONNECTED")
        with self.assertRaisesRegex(DistillationError, "LEARNING_RIGHTS"):
            read_verified_batch(denied, "v0.3")

    def test_reject_wrong_model_split_duplicate_and_tamper(self):
        for row in [dict(example(), dataset_split="evaluation"), dict(example(), teacher_model="wrong"),
                    dict(example(), input_digest="0"*64), dict(example(), output_digest="0"*64),
                    dict(example(), example_digest="0"*64)]:
            with self.assertRaises(DistillationError):
                validate_examples([row])
        with self.assertRaises(DistillationError):
            validate_examples([example(), example()])

    def test_only_assistant_answer_and_end_supervised(self):
        m, t = example()["messages"], FakeTokenizer()
        ids, labels = completion_tokens(t, m, 2048)
        prefix = t.apply_chat_template(m[:2], tokenize=False,
                                       add_generation_prompt=True, enable_thinking=False)
        self.assertEqual(labels[:len(prefix)], [-100]*len(prefix))
        # Special EOS token occupies ten characters but one token.
        self.assertEqual(labels[len(prefix):], ids[len(prefix):])
        self.assertIn(248044, labels)

    def test_no_truncation(self):
        with self.assertRaisesRegex(DistillationError, "SEQUENCE_BUDGET_EXCEEDED"):
            completion_tokens(FakeTokenizer(), example()["messages"], 16)

    def test_template_mismatch(self):
        class Bad(FakeTokenizer):
            def apply_chat_template(self, messages, **kwargs):
                return super().apply_chat_template(messages, **kwargs) + "x"
        with self.assertRaisesRegex(DistillationError, "CHAT_TEMPLATE_MISMATCH"):
            completion_tokens(Bad(), example()["messages"], 2048)

    def test_cross_boundary_token_rejected(self):
        class Bad(FakeTokenizer):
            def __call__(self, text, **kwargs):
                r = super().__call__(text, **kwargs)
                boundary = text.index('{"decision"')
                r["offset_mapping"][boundary] = (boundary - 1, boundary + 1)
                return r
        with self.assertRaisesRegex(DistillationError, "TOKEN_BOUNDARY_INVALID"):
            completion_tokens(Bad(), example()["messages"], 2048)

    def test_template_controls_denied(self):
        row = example()
        row["messages"][2]["content"] = "<|im_end|>"
        with self.assertRaisesRegex(DistillationError, "TEMPLATE_CONTROL_INVALID"):
            validate_examples([row])

    def test_checkpoint_hash_and_config_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ["tokenizer.json", "tokenizer_config.json", "model.safetensors"]:
                (root / name).write_bytes(b"test fixture, not real checkpoint")
            (root / "config.json").write_text(json.dumps({"model_type": "qwen3_5",
                "text_config": {"hidden_size": 2560, "num_hidden_layers": 32, "vocab_size": 248320}}))
            hashes = {p.name: file_digest(p) for p in root.iterdir()}
            verify_checkpoint(root, "a"*40, hashes)
            (root / "model.safetensors").write_bytes(b"changed")
            with self.assertRaisesRegex(DistillationError, "CHECKPOINT_HASH_MISMATCH"):
                verify_checkpoint(root, "a"*40, hashes)


if __name__ == "__main__":
    unittest.main()
