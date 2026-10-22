"""정적 검사: 연결 코드가 닿는 레포 안 파이썬 파일에서 패턴 줄을 찾는다."""
from __future__ import annotations

import ast
import inspect
import re
from collections import defaultdict
from pathlib import Path

from blackclip.check.link import Link

SKIP_DIRS = frozenset({".venv", "venv", "site-packages", "node_modules", ".git"})
"""레포 안이어도 고객 코드가 아닌 폴더."""


def source_files(link: Link) -> list[Path]:
    """link.py · 신호 함수가 정의된 파일 · 연결 모듈이 불러온 것 중 레포 안 .py 파일.

    Args:
        link: 연결 파일.

    Returns:
        경로 목록(정렬). link.py는 늘 들어간다.
    """
    repo = link.repo.resolve()
    found = {link.path.resolve()}
    for value in [link.module.signals, *vars(link.module).values()]:
        mod = value if inspect.ismodule(value) else inspect.getmodule(value)
        f = getattr(mod, "__file__", None)
        if f:
            found.add(Path(f).resolve())
    out = [p for p in found
           if p.suffix == ".py" and repo in p.parents and not SKIP_DIRS & set(p.relative_to(repo).parts[:-1])]
    return sorted(out)


def reachable_lines(files: list[Path], entry: str = "signals") -> dict[Path, set[int]] | None:
    """신호 함수에서 이름으로 닿는 함수 몸통 줄과 각 파일의 최상위 문장 줄(정의 빼고)을 고른다.

    이름이 같은 함수는 모두 닿는다고 본다(호출 대상을 정확히 풀지 않는 근사). 닿는 이름이 클래스면 그 몸통(생성자 · 메서드)도 모두 닿는다고 본다.

    Args:
        files: source_files 결과.
        entry: 시작 함수 이름.

    Returns:
        파일 → 줄 번호 집합. entry 정의를 못 찾으면 None(파일 전체를 본다).
    """
    trees: dict[Path, ast.Module] = {}
    for f in files:
        try:
            trees[f] = ast.parse(f.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
            continue
    defs: dict[str, list[tuple[Path, ast.AST]]] = defaultdict(list)
    lines: dict[Path, set[int]] = defaultdict(set)
    for f, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defs[node.name].append((f, node))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                lines[f].update(range(node.lineno, node.end_lineno + 1))
    todo = [d for d in defs.get(entry, []) if not isinstance(d[1], ast.ClassDef)]
    if not todo:
        return None
    seen: set[int] = set()
    while todo:
        f, node = todo.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        lines[f].update(range(node.lineno, node.end_lineno + 1))
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and sub.id in defs:  # 부름 · 함수 넘기기(apply(f)) · 클래스 만들기 모두
                todo.extend(defs[sub.id])
            elif isinstance(sub, ast.Attribute) and sub.attr in defs:
                todo.extend(defs[sub.attr])
    return dict(lines)


def scan(files: list[Path], repo: Path, patterns: list[tuple[str, str, str | None]],
         only: dict[Path, set[int]] | None = None) -> list[tuple[str, str]]:
    """파일들에서 패턴 줄을 찾는다(# 뒤 주석은 안 본다).

    Args:
        files: 볼 파일.
        repo: 레포 꼭대기(경로를 상대로 적음).
        patterns: (정규식, 이름, 같은 줄에 있으면 빼는 정규식 또는 None).
        only: 파일 → 볼 줄 번호(reachable_lines). None이면 파일 전체.

    Returns:
        ("레포 기준 경로:줄", 이름) 목록(파일 · 줄 순).
    """
    hits: list[tuple[str, str]] = []
    base = repo.resolve()
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        rel = path.relative_to(base).as_posix()
        for no, line in enumerate(lines, 1):
            if only is not None and no not in only.get(path, ()):
                continue
            code = line.split("#", 1)[0]
            for pat, label, unless in patterns:
                if re.search(pat, code) and not (unless and re.search(unless, code)):
                    hits.append((f"{rel}:{no}", label))
    return hits
