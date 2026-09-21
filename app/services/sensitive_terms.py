"""Deterministic sensitive-term replacement, independent of storage and logging.

Rules are dictionaries {id, source, replacement}. Matching is case-sensitive and
literal: a wholly ASCII source requires non-[A-Za-z0-9_] boundaries on both
sides; sources containing non-ASCII characters match substrings. At a position,
the longest source wins. Replacements are emitted once, never scanned recursively.
Existing replacement literals are protected; partial source/alias overlaps fail explicitly. Two sources may share an alias;
counts still belong to their separate IDs, and no reverse mapping is offered.

Duplicate source terms, duplicate IDs, empty terms, and any literal source/alias
containment are rejected. Containment rejection is deliberately conservative:
it also rejects e.g. A -> AA, even though ASCII boundaries could hide A in AA.
If an otherwise valid mapping creates a new boundary match in this particular
text, the whole operation is rejected instead of returning non-idempotent text.
Unmatched characters, whitespace and Unicode forms are preserved byte-for-byte
when encoded using the same encoding. Nothing in this module logs text or terms.
Recursive processing transforms values, never dictionary keys; supported values
are strings, lists, string-keyed dictionaries, int, float, bool and None.
"""
from collections import Counter
from dataclasses import dataclass
import math
import re


class SensitiveTermError(ValueError):
    """Safe error text deliberately excludes terms and document contents."""


@dataclass(frozen=True)
class TermRule:
    id: str
    source: str
    replacement: str


@dataclass(frozen=True)
class CompiledTerms:
    rules: tuple[TermRule, ...]
    source_pattern: re.Pattern | None
    alias_pattern: re.Pattern | None


def is_supported_text(value):
    """XML 1.0 characters also exclude unpaired, non-UTF-8-encodable surrogates."""
    return isinstance(value,str) and all(
        character in '\t\r\n'
        or 0x20 <= ord(character) <= 0xD7FF
        or 0xE000 <= ord(character) <= 0xFFFD
        or 0x10000 <= ord(character) <= 0x10FFFF
        for character in value
    )


def compile_rules(rules):
    if isinstance(rules, CompiledTerms):
        return rules
    if not isinstance(rules, list) or len(rules) > 1000:
        raise SensitiveTermError('词库必须是最多1000条的映射列表')
    items=[]
    ids=set()
    sources=set()
    for raw in rules:
        if not isinstance(raw,dict):
            raise SensitiveTermError('词库映射格式无效')
        values=[raw.get(key) for key in ('id','source','replacement')]
        if any(not is_supported_text(value) or not value.strip() or len(value)>500 for value in values):
            raise SensitiveTermError('词库编号、原词和代称必须为非空有效文本')
        identifier,source,replacement=values
        if identifier in ids:
            raise SensitiveTermError('词库规则编号重复')
        if source in sources:
            raise SensitiveTermError('词库原词重复，请合并或修正映射')
        ids.add(identifier)
        sources.add(source)
        items.append(TermRule(identifier,source,replacement))
    aliases={item.replacement for item in items}
    for source in sources:
        for alias in aliases:
            if source in alias or alias in source:
                raise SensitiveTermError('原词与代称存在匹配碰撞，请调整映射')
    ordered=tuple(sorted(items,key=lambda item:(-len(item.source),item.source,item.id)))
    patterns=[]
    for index,item in enumerate(ordered):
        literal=re.escape(item.source)
        if item.source.isascii():
            literal=r'(?<![A-Za-z0-9_])'+literal+r'(?![A-Za-z0-9_])'
        patterns.append(f'(?P<r{index}>{literal})')
    source_pattern=re.compile('|'.join(patterns)) if patterns else None
    alias_pattern=re.compile('|'.join(re.escape(alias) for alias in sorted(aliases,key=lambda x:(-len(x),x)))) if aliases else None
    return CompiledTerms(ordered,source_pattern,alias_pattern)


def _replace_once(text,compiled):
    if compiled.source_pattern is None:
        return text,{}
    protected=[(match.start(),match.end()) for match in compiled.alias_pattern.finditer(text)]
    output=[]
    counts=Counter()
    cursor=0
    protected_index=0
    for match in compiled.source_pattern.finditer(text):
        while protected_index<len(protected) and protected[protected_index][1]<=match.start():
            protected_index+=1
        if protected_index<len(protected) and protected[protected_index][0]<match.end():
            raise SensitiveTermError('原词与已有代称存在部分交叠，请调整映射或输入')
        item=compiled.rules[int(match.lastgroup[1:])]
        output.extend((text[cursor:match.start()],item.replacement))
        cursor=match.end()
        counts[item.id]+=1
    output.append(text[cursor:])
    return ''.join(output),dict(counts)


def replace_sensitive_terms(text,rules):
    if not isinstance(text,str):
        raise SensitiveTermError('待处理内容必须为文本')
    compiled=compile_rules(rules)
    result,hits=_replace_once(text,compiled)
    # Validate idempotence, but never apply a second replacement to the result.
    checked,_=_replace_once(result,compiled)
    if checked!=result:
        raise SensitiveTermError('替换产生新的边界匹配，请调整碰撞映射')
    return {'text':result,'hits':hits}


def replace_sensitive_fields(value,rules):
    compiled=compile_rules(rules)
    hits=Counter()
    active=set()
    def walk(item,depth):
        if depth>64:
            raise SensitiveTermError('输入结构层级过深')
        if isinstance(item,str):
            result=replace_sensitive_terms(item,compiled)
            hits.update(result['hits'])
            return result['text']
        if item is None or type(item) in (int,bool):
            return item
        if type(item) is float and math.isfinite(item):
            return item
        if type(item) not in (list,dict):
            raise SensitiveTermError('输入包含不支持的数据类型')
        if id(item) in active:
            raise SensitiveTermError('输入不能包含循环引用')
        active.add(id(item))
        try:
            if isinstance(item,list):
                return [walk(child,depth+1) for child in item]
            if any(not isinstance(key,str) for key in item):
                raise SensitiveTermError('输入对象的字段名必须为文本')
            return {key:walk(child,depth+1) for key,child in item.items()}
        finally:
            active.remove(id(item))
    return {'value':walk(value,0),'hits':dict(hits)}
