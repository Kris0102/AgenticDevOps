"""AST-aligned code chunking for the Context Engine.

Uses tree-sitter (prebuilt grammars via tree-sitter-languages) to slice source into
function/class chunks — the natural units agents search for. Anything we can't parse
(unsupported language, parse error, or a file with no top-level definitions) falls back
to a single whole-file chunk, so the chunker is always total and never drops a file.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

# Extension → tree-sitter language name. Extend as more grammars are needed.
LANGUAGE_BY_EXT = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".java": "java",
    ".rb": "ruby",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
}

# tree-sitter node types that represent a searchable definition, per language family.
# Matched by substring so we stay tolerant across grammar versions.
_DEF_KEYWORDS = ("function", "class", "method", "struct", "interface", "impl")

# Files we never chunk: too large, or not source we want indexed.
MAX_FILE_BYTES = 200_000
SKIP_SUFFIXES = (".lock", ".min.js", ".map", ".png", ".jpg", ".jpeg", ".gif",
                 ".pdf", ".zip", ".tar", ".gz", ".ico", ".woff", ".woff2", ".ttf")


@dataclass
class Chunk:
    symbol: Optional[str]
    chunk_type: str  # "function" | "class" | "file"
    chunk_text: str
    start_line: int
    end_line: int


def language_for(filepath: str) -> Optional[str]:
    for ext, lang in LANGUAGE_BY_EXT.items():
        if filepath.endswith(ext):
            return lang
    return None


def should_skip(filepath: str, source: str) -> bool:
    lower = filepath.lower()
    if any(lower.endswith(sfx) for sfx in SKIP_SUFFIXES):
        return True
    if len(source.encode("utf-8", errors="ignore")) > MAX_FILE_BYTES:
        return True
    if "\x00" in source:  # crude binary guard
        return True
    return False


def chunk_file(filepath: str, source: str) -> List[Chunk]:
    """Return AST-aligned chunks for one file, or [] if it should be skipped."""
    if should_skip(filepath, source):
        return []

    lang = language_for(filepath)
    if lang is not None:
        try:
            chunks = _chunk_with_tree_sitter(lang, source)
            if chunks:
                return chunks
        except Exception as e:  # parser missing / grammar error → fall through
            print(f"[Chunker] tree-sitter failed for {filepath} ({e}); using file fallback.")

    return [_whole_file_chunk(source)]


def _chunk_with_tree_sitter(lang: str, source: str) -> List[Chunk]:
    from tree_sitter_languages import get_parser

    parser = get_parser(lang)
    tree = parser.parse(source.encode("utf-8"))
    src_bytes = source.encode("utf-8")
    chunks: List[Chunk] = []

    def is_def(node) -> bool:
        return any(kw in node.type for kw in _DEF_KEYWORDS)

    def emit(node, chunk_type: str) -> None:
        text = src_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
        chunks.append(Chunk(
            symbol=_symbol_name(node, src_bytes),
            chunk_type="class" if "class" in node.type else "function",
            chunk_text=text,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
        ))

    # Top-level definitions, plus methods one level inside a class/impl body.
    for child in tree.root_node.named_children:
        if is_def(child):
            emit(child, child.type)
            if "class" in child.type or "impl" in child.type:
                for grandchild in _body_defs(child):
                    emit(grandchild, grandchild.type)

    return chunks


def _body_defs(node):
    for child in node.named_children:
        # Descend into the block/body node to reach method definitions.
        if child.type in ("block", "class_body", "declaration_list", "impl_item"):
            for inner in child.named_children:
                if any(kw in inner.type for kw in _DEF_KEYWORDS):
                    yield inner


def _symbol_name(node, src_bytes: bytes) -> Optional[str]:
    name = node.child_by_field_name("name")
    if name is not None:
        return src_bytes[name.start_byte:name.end_byte].decode("utf-8", errors="replace")
    return None


def _whole_file_chunk(source: str) -> Chunk:
    return Chunk(
        symbol=None,
        chunk_type="file",
        chunk_text=source,
        start_line=1,
        end_line=source.count("\n") + 1,
    )
