"""Removes secrets and personal identifiers before anything is stored or embedded."""

import re

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("PRIVATE_KEY", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
    ("JWT", re.compile(r"\beyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}\b")),
    ("API_KEY", re.compile(r"\b(?:sk-[\w-]{20,}|ghp_\w{30,}|github_pat_\w{30,}|AKIA[0-9A-Z]{16}|xox[abp]-[\w-]{10,})\b")),
    ("SECRET", re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key)(\s*[=:]\s*)(['\"]?)[^\s'\"]{6,}\3")),
    ("CNPJ", re.compile(r"\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b")),
    ("CPF", re.compile(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b")),
    ("CPF", re.compile(r"(?<![\w.-])\d{11}(?![\w.-])")),
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
]


def redact(text: str) -> str:
    for label, pattern in _PATTERNS:
        if label == "SECRET":
            text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED:{label}]", text)
        else:
            text = pattern.sub(f"[REDACTED:{label}]", text)
    return text
