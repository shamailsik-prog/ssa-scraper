"""
Bench parsing (Annex B-10): judge names, bench_size and bench_type from CORAM / BENCH /
"Before" lines and from explicit bench phrases. Deterministic only.

bench_type ∈ {single, division, full, larger}. bench_size equals the normalised judge count
unless an explicit bench phrase justifies a conflict, in which case the phrase wins and the
conflict is reported.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

HONORIFICS = re.compile(
    r"(?i)\b(mr\.?|mrs\.?|ms\.?|justice|j\.|jj\.|hon'?ble|honourable|honorable|the|chief|acting|c\.?j\.?|cj|hcj|senior puisne judge|judge|judges|sahib|dr\.?)\b"
)
CORAM_LINE = re.compile(r"(?im)^[ \t]*(?:coram|bench|before|present)[ \t]*[:\-—]?[ \t]*([A-Za-z].*?)[ \t]*$")
CORAM_BLOCK = re.compile(r"(?is)\b(?:coram|bench|before)\s*[:\-—]?\s*(.{5,400}?)(?:\n\s*\n|\bpetitioner|\bappellant|\bapplicant|\bversus|\bvs\.?|\bv\.\s)")
JUDGE_SUFFIX = re.compile(r"(?i),?\s*(?:j\.?|jj\.?|c\.?j\.?|cj|hcj|acj|actg\.?\s*c\.?j\.?)\s*(?=$|,|;|\band\b|&)")
SPLIT = re.compile(r"\s*(?:,|;|\band\b|&|\n)\s*", re.I)
JUDGE_CHROME_NOISE = re.compile(r"(?i)(obtaining\s+subscription|update\s+subscriber|^\s*read\s*$)")

BENCH_PHRASES = [
    (re.compile(r"(?i)\b(larger|full)\s+bench\b"), None),
    (re.compile(r"(?i)\bfull\s+bench\b"), "full"),
    (re.compile(r"(?i)\blarger\s+bench\b"), "larger"),
    (re.compile(r"(?i)\bdivision\s+bench\b|\bd\.?b\.?\b(?=\s|$|\))"), "division"),
    (re.compile(r"(?i)\bsingle\s+(?:bench|judge)\b|\bin\s+chambers?\b"), "single"),
]
NUMBER_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "thirteen": 13, "fifteen": 15, "seventeen": 17}
MEMBER_BENCH = re.compile(r"(?i)\b(\d{1,2}|two|three|four|five|six|seven|eight|nine|ten|eleven|thirteen|fifteen|seventeen)[\s\-]*(?:member|judge)s?\s+bench\b")


@dataclass
class BenchInfo:
    judge_names: List[str] = field(default_factory=list)
    bench_size: Optional[int] = None
    bench_type: Optional[str] = None
    explicit_phrase: Optional[str] = None
    conflict: Optional[str] = None
    evidence: Optional[str] = None


def normalise_judge_name(raw: str) -> str:
    s = JUDGE_SUFFIX.sub("", raw or "")
    s = HONORIFICS.sub("", s)
    s = re.sub(r"[^\w\s\.\-']", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" .-")
    return " ".join(part.capitalize() if part.isupper() or part.islower() else part for part in s.split())


def _split_names(blob: str) -> List[str]:
    names: List[str] = []
    for piece in SPLIT.split(blob):
        n = normalise_judge_name(piece)
        if JUDGE_CHROME_NOISE.search(n):
            continue
        if len(n) >= 4 and len(n.split()) <= 6 and not re.search(r"\d", n):
            if n.lower() not in {x.lower() for x in names}:
                names.append(n)
    return names


def bench_type_for_size(size: Optional[int]) -> Optional[str]:
    if not size:
        return None
    if size == 1:
        return "single"
    if size == 2:
        return "division"
    if size in (3, 4, 5):
        return "full"
    return "larger"


def parse_bench(text: str) -> BenchInfo:
    info = BenchInfo()
    if not text:
        return info
    head = text[:6000]
    blob = None
    m = CORAM_LINE.search(head)
    if m:
        blob = m.group(1)
        # CORAM often lists one judge per following line
        start = m.end()
        tail = head[start : start + 600]
        extra_lines = []
        for line in tail.split("\n"):
            line = line.strip()
            if not line:
                break
            if JUDGE_SUFFIX.search(line) or re.search(r"(?i)\bjustice\b", line):
                extra_lines.append(line)
            else:
                break
        if extra_lines:
            blob = blob + "\n" + "\n".join(extra_lines)
    else:
        mb = CORAM_BLOCK.search(head)
        if mb:
            blob = mb.group(1)
    if blob:
        info.evidence = blob.strip()[:400]
        info.judge_names = _split_names(blob)
    # explicit phrases anywhere in the head
    explicit_size = None
    mm = MEMBER_BENCH.search(head)
    if mm:
        token = mm.group(1).lower()
        explicit_size = int(token) if token.isdigit() else NUMBER_WORDS.get(token)
        info.explicit_phrase = mm.group(0)
    explicit_type = None
    for pat, label in BENCH_PHRASES[1:]:
        if pat.search(head):
            explicit_type = label
            info.explicit_phrase = info.explicit_phrase or pat.search(head).group(0)
            break
    counted = len(info.judge_names) or None
    if explicit_size:
        info.bench_size = explicit_size
        if counted and counted != explicit_size:
            info.conflict = f"judge count {counted} differs from explicit phrase '{info.explicit_phrase}'"
    else:
        info.bench_size = counted
    if explicit_type:
        info.bench_type = explicit_type
        derived = bench_type_for_size(info.bench_size)
        if derived and derived != explicit_type and not info.conflict:
            info.conflict = f"derived bench_type {derived} differs from explicit phrase '{info.explicit_phrase}'"
    else:
        info.bench_type = bench_type_for_size(info.bench_size)
    return info
