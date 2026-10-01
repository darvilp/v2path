# v2path

A Python 3.11+ library for HL7 v2 parsing, editing, generation, and optional structural interpretation. No runtime dependencies. Version 0.2.0.

Ordinary manipulation does not require an HL7 version or schema. Unchanged text and byte input retain their original representation. Unknown segments remain editable and serializable.

## Install and edit

From a checkout:

```sh
python -m pip install .
```

```python
from v2path import parse

source = "MSH|^~\\&|SRC|FAC|DST|FAC|20260101||ADT^A01|1|P|2.5.1\rPID|||123||Smith^Jane\r"
msg = parse(source)
assert msg.to_text() == source
msg["PID-5.2"] = "Janet"
assert msg.to_text() == source.replace("Smith^Jane", "Smith^Janet")
```

`parse()` accepts one bare message as `str` or `bytes`. Use `iter_messages()` for paths, file-like objects, chunk iterables, MLLP, or batch files. `Message.new()` creates a minimal editable message. `msg.copy()` creates an independent deep copy.

## Values and references

Fields, components, subcomponents, and repetitions are **one-based**. Segment and group occurrences are **zero-based**.

```python
msg["PID-3[2].1"] = "SECOND_ID"       # second repetition
msg["PID-5"] = "Smith^Janet~Jones^Jo" # replaces the complete field
assert msg["PID-5[2].1"] == "Jones"
msg.set_text("PID-5.1", "Smith^Jones") # literal caret, escaped on the wire
assert msg["PID-5.1"] == "Smith^Jones"
```

- `PID[1]-3[2].1` addresses the second physical PID, second repetition of field 3, component 1.
- A segment-relative reference omits `PID-`, for example `msg.segment("PID")["5.1"]`.
- Omitted repetitions read the first repetition. A field-level setter without brackets replaces the whole field; a component setter changes only its selected repetition.
- Normal reads decode HL7 delimiter escapes and hexadecimal escapes. Compound values retain their structural separators. `get_raw(reference)` returns the original ER7 spelling; at field level without brackets it returns all repetitions.
- Normal assignment interprets separators below the target level. Fields accept repetitions/components/subcomponents; components accept subcomponents. Higher-level unescaped separators raise `AssignmentError` without changing the message.
- `set_text(reference, value)` escapes all literal delimiters and control characters. `set_raw(reference, value)` uses the same structural assignment contract. On a segment, `set_raw(None, er7)` replaces the complete segment.
- MSH delimiter declarations are protected. Use `to_text(delimiters=EncodingCharacters(...))` for an explicit conversion. Converting opaque, unsupported escape sequences fails rather than guessing their meaning.

Singular object lookups take the first match and return `None` when absent. Plural lookups return lists in physical order. A field reference through a missing segment raises `LookupError`; an absent field within a present segment reads as `""` without extending the message.

```python
from v2path import NULL, ValueState

assert msg.segment("OBX") is None
assert msg["PID-99"] == ""
assert msg.value_state("PID-99") is ValueState.ABSENT
msg["PID-7"] = NULL
assert msg["PID-7"] is NULL
assert msg.get_raw("PID-7") == '""'
```

`ValueState` distinguishes `ABSENT`, `EMPTY`, `NULL`, and `VALUE`. Assigning the ER7 token `""` means explicit null; `set_text(..., '""')` stores two literal quote characters.

## Views and groups

A view interprets the same physical segments. Field edits are shared between views, while each view owns its groups and attachments. Serialization always follows physical order.

```python
from v2path import Grammar, GroupRule

msg.append("ORC|1")
msg.append("OBX|1|ST|CODE||result")
msg.append("ZXX|vendor content")
msg.append("NTE|1||note")
view = msg.resolve()
assert view.segment("NTE").parent().name == "OBX"

rules = Grammar((GroupRule("ORDER", "ORC", children=(
    GroupRule("OBSERVATION", "OBX"),
)),))
view = msg.resolve(rules)
assert view["ORDER[0]/OBSERVATION[0]/OBX-5"] == "result"
print(view.structure())
```

Default NTE attachment selects the nearest recognized native non-NTE segment in the current scope, skipping Z-segments. NTE after ORC attaches to ORC even when a standard definition would disallow that placement. Consecutive NTEs share their anchor. No standard placement warning is emitted.

Custom `Grammar(attachments={"NTE": ("ZXX",)})` makes the nearest ZXX in scope the note's anchor. `native_segments` can extend the initial vocabulary. With no group grammar, the scope is the whole message.

`GroupRule(name, start, end_before=(), children=())` defines repeating boundaries. `start` accepts a segment name, tuple of names, or a callable receiving `BoundaryContext(segment, current, parent)`. `current` is the active child group at that scope or `None`. Sibling starts and parent closure end existing groups. Boundaries exclude their `end_before` segment. Static conflicts are rejected; runtime ambiguity produces one diagnostic and retains the physical content.

`group()`/`groups()` and `segment()`/`segments()` operate on direct members. Use `recursive=True` on plural lookups to search descendants. Structural paths traverse direct scopes. `children()`, `child()`, `parent()`, and `group()` on a view-bound segment expose relationships. `next()`, `previous()`, and `until()` expose physical navigation.

### Mutation and stale views

Use `msg.insert(index, er7)`, `append(er7)`, `remove(segment)`, and `move(segment, index)` for physical edits. A move index refers to the physical list before removal. `group.move(index)` moves the complete physical range, including vendor segments; `group.append(other_group)` moves a same-message group to the target's end. Cross-message movement is rejected.

`obx.add_child("NTE", "1||new note")` inserts after its last attached NTE, or immediately after the anchor if none exists. Existing unknown segments retain their relative order.

Insertion, deletion, movement, and segment identifier changes invalidate **all** existing views. Structural navigation then raises `StaleViewError`; resolve explicitly again. Raw value access remains available. Field edits do not invalidate v0.1 boundary grammars, which must depend only on segment identifiers and group membership, not field values.

Diagnostics are available in `msg.diagnostics` and `view.diagnostics`. They never print, log, or emit Python warnings automatically. Unknown Z-segments and deterministic first-match lookups are not diagnostics.

### Bundled presets

```python
from v2path import presets

view = msg.resolve(presets.ORM_O01)
orders = view.groups("ORDER")
```

Available presets are `ADT_A01`, `SIU_S12`, `ORM_O01`, `ORU_R01`, and `DFT_P03`. They are lightweight boundary interpretations, not complete standard validators. Select them explicitly; default parsing/resolution does not select a schema using MSH metadata.

The native vocabulary and preset reference structures start from HL7 2.5.1. Messages declaring other versions remain manipulable. Provenance and the retained MIT notice are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## File conversion

```python
from v2path import transform_stream

def convert(msg):
    patient = msg.segment("PID")
    if patient is not None:
        patient["3.1"] = "REPLACEMENT_ID"
    return msg

# Paths can be strings or pathlib.Path objects.
# transform_stream("input.hl7", "output.hl7", convert)
```

A callback returns one message, an iterable of messages, or `None` to filter it. Use `msg.copy()` for independent output variants. Aggregation is application-managed state.

`transform_stream()` preserves source segment terminators, framing, and batch/file envelopes by default. It updates affected batch message counts after filtering or splitting and retains empty batches. An untouched stale count remains unchanged until a transformation changes its associated content.

| Option | Choices |
|---|---|
| `terminator` | `None` to preserve, `"\r"`, `"\n"`, `"\r\n"` |
| `framing` | `"preserve"`, `"bare"`, `"mllp"` |
| `envelopes` | `"preserve"`, `"omit"`, `"generate"` |
| `encoding` | Explicit source encoding override |
| `output_encoding` | Explicit output character encoding |
| `delimiters` | Explicit `EncodingCharacters` override for messages and envelopes |

Generating envelopes requires `batch_header`, a complete BHS segment supplied by the caller. Optional `file_header` supplies FHS. BTS and FTS are generated from output counts. Business metadata is never invented. MLLP's final CR is independent of the ER7 terminator option. For TCP listeners, ACKs, and delivery policies, use the optional-import network and service modules described below.

```python
from io import BytesIO

out = BytesIO()
transform_stream(source, out, convert, terminator="\r\n", framing="mllp",
                 envelopes="generate", batch_header="BHS|^~\\&|SOURCE")
```

`iter_messages()` is a message-only convenience; manually writing its results does not reproduce file envelopes. Use `iter_records()` and `MessageWriter.write_record()` for complete records, or `transform_stream()` for envelope-aware conversion. `MessageWriter.write(msg)` is the simple message writer. Both writers require a context manager and binary output streams.

Path destinations are written to a temporary sibling and replaced only after successful conversion. Exceptions leave an existing destination untouched. Caller-owned output streams can contain partial output after an exception. Inputs are read incrementally, buffering one message plus a physical record/read chunk.

A missing final terminator is preserved for an untouched message. If a later record needs separation, the writer inserts the preceding terminator style, falling back to CR.

### Encodings and limits

Support covers ASCII, UTF-8, ISO-8859-1, and Windows-1252. Byte parsing chooses an explicit override, then recognized MSH-18, then UTF-8 with reversible handling of undecodable source bytes. Unsupported declared encodings require an override. Already-decoded text is not reinterpreted using a byte codec.

`msg.to_bytes(encoding=...)` converts text and hex escapes and updates MSH-18. New values are encoded strictly, never replaced silently. Direct conflicting MSH-18 assignments are rejected. Source bytes that cannot be interpreted prevent an explicit charset conversion.

Envelope records have no reliable charset declaration. Non-ASCII text envelopes or explicit conversion of non-ASCII byte envelopes require an explicit source encoding. Delimiter conversion updates message and envelope declarations, data, and matching trailers. Stateful encodings, UTF-16/UTF-32, sequence grammars, field-dependent boundaries, and full datatype/table validation are outside the current release.

## Network and routed services

Version 0.2 adds `v2path.ack`, `v2path.net`, and `v2path.service` in the same distribution. They use only the Python standard library. Imports start no sockets or event loops. See the [runtime guide](docs/runtime.md) for ACK policy, routing, file publication, limits, and recovery.

Run the self-contained loopback example after installation:

```sh
python examples/mllp_route.py
```

## Development

```sh
python -m pip install -e '.[dev]'
python -m pytest -q
python -m build
```

CI covers Python 3.11–3.14 on Linux and Python 3.11 on Windows. Tests cover preservation, values, views, group ranges, presets, framing/envelopes, chunk boundaries, atomic output failure, live ACK combinations, persistent sockets, fanout, retries, directory chaining, failure replay, and resumable shutdown, with Hypothesis round-trip properties.

The [design document](base%20design.md) records the agreed behavior and implementation decisions.

## License

Copyright V2Path contributors. V2Path is free software: you may redistribute and/or modify it under the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or, at your option, any later version. V2Path is distributed without any warranty. See [LICENSE](LICENSE).
