"""Deterministic full-prompt token budgeting, shared by every endpoint."""
from functools import lru_cache
from pathlib import Path

TOKENIZER = "o200k_base"


@lru_cache(maxsize=1)
def encoding():
    import tiktoken
    return tiktoken.get_encoding(TOKENIZER)


def count(text):
    return len(encoding().encode(text, disallowed_special=()))


def make_prompt(task, target, config_dir):
    base = Path(config_dir)
    template = (base / task["template_file"]).read_text(encoding="utf-8")
    corpus = (base / task["corpus_file"]).read_text(encoding="utf-8")
    if template.count("{{context}}") != 1:
        raise ValueError("任务模板必须恰好包含一个 {{context}}")
    if target is None:
        return template.replace("{{context}}", corpus)
    reserve = count(template.replace("{{context}}", "")) + 8
    if target < reserve:
        raise ValueError(f"目标 Token 数 {target} 小于模板所需空间 {reserve}")
    if not corpus.strip():
        raise ValueError("公共语料不能为空")
    unit = corpus + "\n"
    repeats = max(1, target // max(1, count(unit)) + 2)
    material = encoding().encode(unit * repeats, disallowed_special=())
    budget = target - reserve
    while True:
        context = encoding().decode_bytes(material[:budget]).decode("utf-8", errors="ignore")
        prompt = template.replace("{{context}}", context + "\n")
        gap = target - count(prompt)
        if gap >= 0:
            break
        budget = max(0, budget + gap - 1)
    # Each ' x' is one token in this encoding; verify the complete template,
    # including both boundaries, rather than assuming additive token counts.
    for adjustment in range(-4, 9):
        padding = gap + adjustment
        if padding < 0:
            continue
        prompt = template.replace("{{context}}", context + "\n" + " x" * padding)
        if count(prompt) == target:
            return prompt
    raise ValueError(f"无法精确构造 {target} Token 的输入，请调整模板或语料")
