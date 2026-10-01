"""Optional boundary-based structural interpretations over physical segments."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from .core import Message, Segment
from .errors import AssignmentError, Diagnostic, StaleViewError
from .native import NATIVE_SEGMENTS


@dataclass(frozen=True)
class BoundaryContext:
    segment: Segment
    current: Group | None
    parent: Group


@dataclass(frozen=True)
class GroupRule:
    name: str
    start: str | tuple[str, ...] | Callable[[BoundaryContext], bool]
    end_before: tuple[str, ...] = ()
    children: tuple[GroupRule, ...] = ()

    def __post_init__(self):
        if not re.fullmatch('[A-Z][A-Z0-9_]*', self.name):
            raise ValueError('Group names must be uppercase semantic identifiers')
        if isinstance(self.start, str):
            object.__setattr__(self, 'start', (self.start,))
        elif not callable(self.start):
            object.__setattr__(self, 'start', tuple(self.start))
        object.__setattr__(self, 'children', tuple(self.children))
        object.__setattr__(self, 'end_before', tuple(self.end_before))


@dataclass(frozen=True)
class Grammar:
    groups: tuple[GroupRule, ...] = ()
    native_segments: frozenset[str] = NATIVE_SEGMENTS
    attachments: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, 'groups', tuple(self.groups))
        object.__setattr__(self, 'native_segments', frozenset(self.native_segments))
        # Copy caller data; a resolved view takes its own interpretation snapshot.
        object.__setattr__(self, 'attachments', {k: tuple(v) for k, v in self.attachments.items()})
        def check(rules, ancestors):
            starts = set()
            names = set()
            for rule in rules:
                if id(rule) in ancestors:
                    raise ValueError('Recursive group rule cycle')
                if rule.name in names:
                    raise ValueError('Sibling group rules must have distinct names')
                names.add(rule.name)
                if not callable(rule.start):
                    if starts.intersection(rule.start):
                        raise ValueError('Sibling rules have conflicting start segments')
                    starts.update(rule.start)
                check(rule.children, ancestors | {id(rule)})
        check(self.groups, set())


class SegmentView:
    def __init__(self, view, raw):
        self._view, self.raw = view, raw

    @property
    def name(self):
        return self.raw.name

    def __getitem__(self, ref):
        return self.raw[ref]

    def __setitem__(self, ref, value):
        self.raw[ref] = value

    def get_raw(self, ref=None):
        return self.raw.get_raw(ref)

    def set_raw(self, ref, value):
        self.raw.set_raw(ref, value)

    def set_text(self, ref, value):
        self.raw.set_text(ref, value)

    def value_state(self, ref):
        return self.raw.value_state(ref)

    def parent(self):
        self._view._check()
        raw = self._view._parents.get(self.raw)
        return self._view._wrap(raw) if raw else None

    def children(self, name=None):
        self._view._check()
        return [self._view._wrap(s) for s in self._view._attachments.get(self.raw, ()) if name is None or s.name == name]

    def child(self, name, occurrence=0):
        return _nth(self.children(name), occurrence)

    def attachments(self):
        return self.children()

    def group(self):
        self._view._check()
        return self._view._owners[self.raw]

    def add_child(self, name, value=''):
        self._view._check()
        if not re.fullmatch('[A-Z0-9]{3}', name):
            raise ValueError('Invalid segment name')
        children = self._view._attachments.get(self.raw, [])
        index = self.raw.message.segments().index(children[-1] if children else self.raw) + 1
        enc = self.raw.message.delimiters
        return self.raw.message.insert(index, name + (enc.field + value if value else ''))

    def remove(self):
        self.raw.remove()

    def next(self, name=None):
        return self.raw.next(name)

    def previous(self, name=None):
        return self.raw.previous(name)

    def until(self, name):
        return self.raw.until(name)

    def __repr__(self):
        return f'SegmentView({self.name!r})'


def _nth(items, index):
    if index < 0:
        raise ValueError('Occurrences must be zero-based and nonnegative')
    return items[index] if index < len(items) else None


class Group:
    def __init__(self, view, name, rule, parent, start):
        self._view, self.name, self._rule, self._parent = view, name, rule, parent
        self._start = self._end = start
        self._children, self._segments = [], []

    def groups(self, name=None, *, recursive=False):
        self._view._check()
        result = []
        for child in self._children:
            if name is None or child.name == name:
                result.append(child)
            if recursive:
                result.extend(child.groups(name, recursive=True))
        return result

    def group(self, name, occurrence=0):
        return _nth(self.groups(name), occurrence)

    def segments(self, name=None, *, recursive=False):
        self._view._check()
        raw = self._view.message.segments()[self._start:self._end] if recursive else self._segments
        return [self._view._wrap(s) for s in raw if name is None or s.name == name]

    def segment(self, name, occurrence=0):
        return _nth(self.segments(name), occurrence)

    @property
    def start_segment(self):
        self._view._check()
        raw = self._view.message.segments()
        return self._view._wrap(raw[self._start]) if self._start < self._end else None

    @property
    def anchor(self):
        return self.start_segment

    def segment_types(self):
        return list(dict.fromkeys(s.name for s in self.segments(recursive=True)))

    def groups_containing(self, name):
        return [g for g in self.groups(recursive=True) if any(s.name == name for s in g.segments())]

    def _target(self, path):
        self._view._check()
        parts = path.split('/')
        scope = self
        for part in parts[:-1]:
            m = re.fullmatch(r'([A-Z][A-Z0-9_]*)(?:\[(\d+)\])?', part)
            if not m:
                raise ValueError('Invalid group path')
            scope = scope.group(m[1], int(m[2] or 0))
            if scope is None:
                raise LookupError(f'Missing group: {part}')
        from .core import reference
        r = reference(parts[-1])
        if not r.segment:
            raise ValueError('Structural field paths require a segment')
        segment = scope.segment(r.segment, r.occurrence)
        if segment is None:
            raise LookupError(f'Missing segment {r.segment}')
        return segment, parts[-1].split('-', 1)[1]

    def __getitem__(self, path):
        segment, ref = self._target(path)
        return segment[ref]

    def __setitem__(self, path, value):
        segment, ref = self._target(path)
        segment[ref] = value

    def set_text(self, path, value):
        segment, ref = self._target(path)
        segment.set_text(ref, value)

    def get_raw(self, path):
        segment, ref = self._target(path)
        return segment.get_raw(ref)

    def value_state(self, path):
        segment, ref = self._target(path)
        return segment.value_state(ref)

    def append(self, value):
        self._view._check()
        if isinstance(value, Group):
            value._view._check()
            if value._view.message is not self._view.message:
                raise AssignmentError('Cannot move groups across messages; copy their content')
            if value._start <= self._start < value._end:
                raise AssignmentError('Cannot move a group into itself or its descendants')
            self._view.message._move_range(self._view.message.segments()[value._start:value._end], self._end)
            return None
        return self._view.message.insert(self._end, value)

    def move(self, index):
        self._view._check()
        self._view.message._move_range(self._view.message.segments()[self._start:self._end], index)

    def remove(self):
        self._view._check()
        items = self._view.message.segments()[self._start:self._end]
        if any(s.name == 'MSH' for s in items):
            raise AssignmentError('Cannot remove a group containing MSH')
        for s in items:
            self._view.message.remove(s)

    def structure(self):
        self._view._check()
        lines = []
        def show(group, depth):
            lines.append('  ' * depth + group.name)
            entries = [(g._start, g) for g in group._children]
            positions = {s: i for i, s in enumerate(self._view.message.segments())}
            entries += [(positions[s], s) for s in group._segments]
            for _, item in sorted(entries, key=lambda e: e[0]):
                if isinstance(item, Group):
                    show(item, depth + 1)
                else:
                    parent = self._view._parents.get(item)
                    lines.append('  ' * (depth + 1) + item.name + (f' -> {parent.name}' if parent else ''))
        show(self, 0)
        return '\n'.join(lines)


class ResolvedMessage(Group):
    def __init__(self, message: Message, grammar=None):
        if grammar is None:
            grammar = Grammar()
        if not isinstance(grammar, Grammar):
            raise TypeError('grammar must be a Grammar')
        self.message = message
        self._revision = message._revision
        self.diagnostics = []
        self._parents, self._attachments, self._owners, self._wrappers = {}, {}, {}, {}
        self._ambiguous = set()
        super().__init__(self, 'MESSAGE', None, None, 0)
        self._resolve_groups(grammar)
        last = {}
        eligible = set(grammar.native_segments) - {'NTE'}
        overrides = grammar.attachments
        # Keep candidates by direct scope. Closed siblings can never leak anchors.
        seen = {}
        reported = set()
        blocked = set()
        for i, raw in enumerate(message.segments()):
            scope = self._owners[raw]
            candidates = seen.setdefault(scope, [])
            if raw in self._ambiguous:
                candidates.clear()
                last.pop(scope, None)
                blocked.add(scope)
                continue
            if raw.name in overrides or raw.name == 'NTE':
                if scope in blocked:
                    continue
                parent = next((s for s in reversed(candidates) if s.name in overrides[raw.name]), None) if raw.name in overrides else last.get(scope)
                if parent is None:
                    key = (scope, raw.name)
                    if key not in reported:
                        self.diagnostics.append(Diagnostic('unattached', f'No eligible anchor for {raw.name} in {scope.name}', i))
                        reported.add(key)
                else:
                    self._parents[raw] = parent
                    self._attachments.setdefault(parent, []).append(raw)
            elif raw.name in eligible:
                blocked.discard(scope)
                last[scope] = raw
            candidates.append(raw)

    def _check(self):
        if self._revision != self.message._revision:
            raise StaleViewError('Message topology changed; call message.resolve() again')

    def _wrap(self, raw):
        if raw not in self._wrappers:
            self._wrappers[raw] = SegmentView(self, raw)
        return self._wrappers[raw]

    def _resolve_groups(self, grammar):
        stack = [self]
        rows = self.message.segments()
        def close(depth, index):
            for group in stack[depth:]:
                group._end = index
            del stack[depth:]
        def matches(rule, raw, current, parent):
            return rule.start(BoundaryContext(raw, current, parent)) if callable(rule.start) else raw.name in rule.start
        for index, raw in enumerate(rows):
            for depth in range(1, len(stack)):
                if raw.name in stack[depth]._rule.end_before:
                    close(depth, index)
                    break
            opened = None
            conflict = False
            # Restart an active ancestor first; otherwise prefer the deepest
            # applicable scope so specimen/financial child rules stay reachable.
            choices = []
            for depth, parent in enumerate(list(stack)):
                rules = grammar.groups if parent is self else parent._rule.children
                current = stack[depth + 1] if depth + 1 < len(stack) else None
                found = [r for r in rules if matches(r, raw, current, parent)]
                if found:
                    choices.append((depth, parent, current, found))
            restarts = [c for c in choices if c[2] is not None and c[2]._rule in c[3]]
            chosen = restarts[0] if restarts else choices[-1] if choices else None
            if chosen:
                depth, parent, current, found = chosen
                close(depth + 1, index)
                if len(found) > 1:
                    self.diagnostics.append(Diagnostic('ambiguous_boundary', 'Multiple group rules match; content left unresolved', index))
                    self._ambiguous.add(raw)
                    conflict = True
                else:
                    opened = Group(self, found[0].name, found[0], parent, index)
                    parent._children.append(opened)
                    stack.append(opened)
            if opened is not None and not conflict:
                while True:
                    found = [r for r in opened._rule.children if matches(r, raw, None, opened)]
                    if len(found) > 1:
                        self.diagnostics.append(Diagnostic('ambiguous_boundary', 'Multiple child groups match; content left unresolved', index))
                        self._ambiguous.add(raw)
                        break
                    if not found:
                        break
                    child = Group(self, found[0].name, found[0], opened, index)
                    opened._children.append(child)
                    stack.append(child)
                    opened = child
            stack[-1]._segments.append(raw)
            self._owners[raw] = stack[-1]
        close(1, len(rows))
        self._end = len(rows)
