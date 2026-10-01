# V2Path Design

**Status:** Reviewed design, ready for implementation planning\
**Project:** `v2path`\
**Primary language:** Python 3.11+\
**License:** GPL-3.0-or-later\
**Primary use case:** HL7 v2 file-based parsing, transformation, and generation\
**Long-term direction:** Optional lightweight always-running interface runtime

## 1. Purpose

V2Path is a lightweight HL7 v2 parsing and transformation library aimed at the gap between simple ER7 manipulation libraries such as `hl7lw` and schema-heavy libraries such as `hl7apy`.

The desired experience is:

- convenient and tolerant for ordinary message manipulation;
- aware of repeating and nested message structure when structure matters;
- usable without loading or satisfying a complete HL7 schema;
- lossless for untouched source content, including unknown, vendor-specific, or structurally unusual segments;
- particularly well suited to file-based conversions;
- reusable later inside an always-running listener/router/transform/writer service.

V2Path is a generic library, not a converter dedicated to one message type. A common workflow modifies a message while retaining its type, such as ADT to ADT. Examples and fixtures should prove behavior without restricting the library to those conversions.

The central design problem is **structural association**.

A flat message model can easily answer:

```python
msg.segments("NTE")
```

but cannot reliably answer:

> Which PID, OBR, OBX, SPM, or other structure does this NTE belong to?

V2Path therefore uses **anchor attachment as its default structural grammar**, with **explicit semantic groups available when the message is complex, ambiguous, or vendor-specific**.

---

## 2. Design principles

### Parse first, resolve structure second

ER7 parsing and HL7 structural interpretation are separate operations.

A message should normally parse even when:

- its structure is non-standard;
- it contains Z-segments;
- a segment occurs somewhere unexpected;
- expected standard segments are absent;
- the declared version or message type is inaccurate;
- no schema is available.

Structural resolution adds meaning to the parsed message. It does not determine whether the underlying message is allowed to exist.

### Preserve the physical message

The serialized segment sequence remains authoritative.

Groups and anchor relationships are structural views over the original segments rather than transformed copies.

Unknown and unresolved segments remain present and serializable.

The default contract is **preserve what the caller did not change; normalize only on request**:

- Untouched text input renders as identical text, including delimiters, escape spelling, trailing empty fields, and line endings.
- Untouched byte input renders as identical bytes when retaining the original encoding and output format.
- A targeted edit preserves source representation outside the affected portion.
- Absent values, empty values, and HL7 explicit null (`""`) remain distinguishable in the model, even where convenient value access treats absent and empty alike.
- Character encoding, delimiters, segment terminators, framing, and envelopes change only through explicit output choices or necessary consequences of an edit.

For batch transformations, affected counts must reflect the resulting content rather than preserve stale values. Exact preservation does not justify incorrect metadata after an edit.

### Anchors handle the ordinary case

Most transformation logic naturally thinks in relationships such as:

- these NTEs belong to this OBX;
- this NTE belongs to this OBR;
- these segments belong to this PID;
- these observations belong to this order.

These relationships should not require a complete message schema.

### Explicit groups handle the difficult case

Anchor inference will not resolve every structure correctly.

For complicated, ambiguous, or vendor-specific messages, callers can provide lightweight structural rules describing:

- group boundaries;
- nesting;
- repetition;
- segment ordering where necessary.

These rules describe structure without requiring complete datatype, table, or cardinality validation.

### Standards knowledge is optional

Ordinary parsing, editing, serialization, and positional attachment are version-independent. They do not require selecting an HL7 version or satisfying a standard message definition.

HL7 2.5.1 supplies the initial vocabulary of recognized native segments and a reference for bundled structures. It is not an acceptance gate. Unfamiliar segments remain present and editable, and inaccurate or unsupported MSH version metadata does not prevent ordinary manipulation.

Bundle lightweight structures for these initial common events:

- `ADT^A01`: admit/visit notification;
- `SIU^S12`: new appointment booking notification;
- `ORM^O01`: general order message;
- `ORU^R01`: unsolicited observation results;
- `DFT^P03`: post detail financial transaction.

These are optional interpretation aids, not claims of complete coverage of every event or version in those families. Full schema, datatype, and table validation remain separate concerns.

### File conversion comes first

The initial project should concentrate on:

```text
read
→ parse
→ resolve
→ transform
→ write
```

Networking, listener lifecycle, persistence, retries, ACK management, and service operation are deliberately later concerns.

---

# 3. Core object model

## Raw document model

The lowest layer represents ER7 syntax:

```text
Message
  segments[]
    Segment
      fields[]
        repetitions[]
          components[]
            subcomponents[]
```

The message owns its encoding information:

```text
EncodingCharacters
  field
  component
  repetition
  escape
  subcomponent
```

Delimiter and source-encoding state belongs to each message and should be immutable. Preserve source segment terminators separately, including mixed terminators and an absent final terminator; a single normalized terminator must not erase that information. Output overrides do not mutate the original parsing state.

A reusable parser must not change the interpretation of previously parsed messages when it encounters a later MSH with different delimiters.

## Structural view

A resolved message overlays structural information on the raw message:

```text
ResolvedMessage
  Group
    Group
      Segment
      Attachment
```

These objects reference the existing `Segment` instances.

They do not duplicate the physical message.

A grammar defines interpretation rules; a resolved view records their results for one message. Multiple views can coexist over the same raw message, for example default and vendor-specific interpretations.

Group membership and attachment relationships belong to each view, not to shared raw segments. Relationship methods such as `parent()`, `children()`, and `group()` operate on view-bound segment references. The exact wrapper API remains an implementation detail.

A field edit through any view changes the shared underlying segment, and other views see that value. Resolving a different interpretation does not change physical order or serialization. Independent transformed outputs require copying the message, not merely creating another view.

For example:

```text
raw order:

ORC
OBR
NTE
OBX
NTE
NTE
OBX
NTE
```

could have a resolved view resembling:

```text
ORDER
  ORC
  OBR
    NTE

  OBSERVATION
    OBX
      NTE
      NTE

  OBSERVATION
    OBX
      NTE
```

while serialization still uses the original segment sequence.

---

# 4. Anchor attachment

Anchor attachment is V2Path's default structural mechanism. The default NTE rule is positional, independent of whether that placement is valid in a standard message grammar:

> Attach NTE to the nearest preceding recognized native, non-NTE segment within the current structural scope.

Use the initial native-segment vocabulary described in section 2. Skip Z-segments and other unrecognized segments by default without removing or rewriting them. Consecutive NTEs share the same anchor rather than attaching to one another. Custom rules can explicitly recognize vendor segments as anchors.

| Physical sequence | Default relationship |
|---|---|
| `OBR → OBX → ZXX → NTE` | NTE attaches to OBX. ZXX remains in place. |
| `OBR → OBX → ORC → NTE → OBR` | NTE attaches to ORC. The later OBR does not change it. |
| `OBR → OBX → NTE` | NTE attaches to OBX. |
| `OBX → NTE → NTE` | Both NTEs attach to OBX. |

All these relationships are deterministic and produce no attachment warning. Validating standard segment placement is a separate, explicit operation. Default attachment must not reject ORC as an anchor merely because a standard grammar would disallow an NTE there.

## Anchor scope

Explicit groups constrain attachment. A search cannot reach backward into an earlier group. If no eligible anchor exists in the current scope, leave the NTE unattached and make that condition available as a diagnostic.

Without a group grammar, the raw message is the scope. Each newly encountered eligible native segment replaces the current anchor. The default algorithm does not infer a complete order or patient hierarchy from this alone.

When groups are supplied, resolve their boundaries before resolving attachments within those scopes:

```text
raw message
    ↓
group resolution, when supplied
    ↓
anchor resolution within scopes
    ↓
resolved message
```

The implementation can use a context stack for nested groups. Optimization into a single pass is unnecessary unless profiling justifies it.

## Anchor API

Relationships are accessible through view-bound segment references. Illustrative API:

```python
resolved = msg.resolve()
obx = resolved.segment("OBX")
notes = obx.children("NTE")
notes[0].parent()
```

Physical navigation remains available separately, without requiring a path expression:

```python
obx.next("NTE")
obr.until("OBX")
segment.previous("ORC")
```

A physical neighbor and an attached child are distinct concepts, particularly when Z-segments occur between an anchor and its NTEs.

---

# 5. Explicit structural groups

Anchors intentionally do not solve every structure.

V2Path therefore supports explicit nested and repeating groups.

The first release uses boundary rules, not a sequence grammar:

```python
from v2path import Grammar, GroupRule

rules = Grammar((
    GroupRule("ORDER", "ORC", children=(
        GroupRule("OBSERVATION", "OBX"),
    )),
))
resolved = msg.resolve(rules)
```

`start` accepts a segment name, tuple of names, or a boundary predicate receiving the incoming segment and current group context. `end_before` closes a group before specified segment types. Repeated starts open sibling groups, and parent closure closes its descendants.

Boundary predicates in the first release depend only on segment identifiers and group membership. Field-dependent rules and an `optional`/`required`/`many` sequence language remain later work.

This grammar describes **message structure only**.

It does not require definitions for:

```text
OBX-2 datatype
PID-5 components
HL7 tables
coded values
maximum field lengths
```

Those are separate validation concerns.

## Mixed anchor/group behavior

Groups and anchors complement each other.

Groups answer:

> What structural scope am I in?

Anchors answer:

> Which nearby segment owns this segment?

For example:

```text
ORDER[0]
  ORC
  OBR
    NTE

  OBSERVATION[0]
    OBX
      NTE
      NTE

  OBSERVATION[1]
    OBX
      NTE
```

The group grammar establishes `ORDER` and `OBSERVATION`.

The anchor grammar establishes the NTE relationships.

## Precedence

When structural mechanisms overlap:

```text
explicit group structure
        ↓
anchor attachment
        ↓
relative/raw position
```

Explicit structure constrains anchors rather than disabling them.

This allows custom rules to be narrowly applied to only the unusual portion of a message.

---

# 6. Group naming

## Recommendation: semantic group names

Canonical group names should represent semantic containers:

```text
PATIENT
PATIENT_VISIT
ORDER
ORDER_OBSERVATION
OBSERVATION
SPECIMEN
INSURANCE
FINANCIAL
PATIENT_RESULT
```

Segment names continue to represent actual HL7 segments:

```text
PID
PV1
ORC
OBR
OBX
SPM
IN1
```

This distinction should be intentional.

For example:

```text
PID != PATIENT
```

A `PID` is one physical segment.

A `PATIENT` group might contain:

```text
PATIENT
  PID
  PD1
  NTE
  NK1
  ...
```

depending on the message structure.

Using `PID` as the group name would make simple examples initially easier, but it creates ambiguity once a group contains several segments or several structural groups contain the same segment type.

Semantic names therefore appear to be the better long-term convention.

## Standard naming

Where the HL7 message definition already provides a useful group name, V2Path should generally use it.

Names should be:

- uppercase;
- underscore-separated;
- semantic;
- stable across APIs.

Examples:

```text
PATIENT
PATIENT_VISIT
ORDER_OBSERVATION
OBSERVATION
SPECIMEN
```

Custom grammars follow the same convention.

The precise standard-name vocabulary should be tested during the first prototype rather than treated as irrevocable before using the API against real messages.

## Making semantic names discoverable

The downside is newcomer opacity.

Someone who knows HL7 segments may naturally look for PID rather than PATIENT.

V2Path should solve that through introspection rather than conflating the two concepts.

A structure display could show:

```text
PATIENT<PID>
ORDER<ORC, OBR>
OBSERVATION<OBX>
SPECIMEN<SPM>
```

Here:

- `PATIENT` is the group name;
- `PID` is its principal/anchor segment.

The angle-bracket notation is descriptive output, not canonical path syntax.

Useful API properties on resolved groups and view-bound segments could include:

```python
group.name
group.anchor
group.start_segment
group.segment_types()

segment.group()

resolved.groups()
resolved.structure()
```

For example:

```python
print(resolved.structure())
```

might produce:

```text
MESSAGE
├── PATIENT<PID>
│   └── NTE
│
└── ORDER<ORC,OBR>[0]
    ├── OBSERVATION<OBX>[0]
    │   ├── NTE
    │   └── NTE
    │
    └── OBSERVATION<OBX>[1]
```

The documentation should repeatedly reinforce one rule:

> **Segments are physical HL7 records. Groups are semantic containers.**

## Avoid implicit aliases

V2Path should probably **not** silently interpret:

```python
resolved.group("PID")
```

as:

```python
resolved.group("PATIENT")
```

That becomes ambiguous as structures become more complicated.

Instead, the relationship should be discoverable:

```python
resolved.groups_containing("PID")
```

or:

```python
pid.group()
```

Custom grammars could support explicit aliases if a particular application needs them.

---

# 7. References and paths

V2Path should provide several complementary addressing mechanisms.

No single path syntax needs to solve every use case.

## Familiar field references

Simple manipulation should remain simple:

```python
msg["PID-3.1"]
segment["5.1"]
```

Singular access takes the first match in physical order within the relevant scope. `msg["PID-3.1"]` uses the first physical PID. A singular group or segment lookup within a resolved group takes its first matching member. Multiple matches do not produce a warning; callers use plural access or explicit occurrences when needed.

For a present segment, convenient value access returns `""` for either an absent or empty field value. Explicit HL7 null reads as the exported `NULL` singleton. A separate presence/state API distinguishes absent, empty, and explicit HL7 null. Reading an absent field must not create it or change serialization. A missing segment or group remains distinguishable from a present segment with an empty field; singular object lookup returns `None`, while a field path through a missing object raises `LookupError`.

## Flat occurrence access

Callers should still be able to deliberately ignore structure:

```python
msg.segments("OBX")[2]
```

This clearly means:

> third physical OBX in serialization order.

It avoids overloading structural path indexes with global occurrence semantics.

## Structural paths

Resolved structures can use semantic paths:

```text
PATIENT[0]/PID-3.1

ORDER[1]/OBSERVATION[2]/OBX-5.1

ORDER_OBSERVATION[0]/SPECIMEN[1]/SPM-2.1
```

Indexes are relative to the containing structural scope.

Equivalent object navigation might be:

```python
resolved.group("ORDER", 1) \
   .group("OBSERVATION", 2) \
   .segment("OBX")
```

## Anchor-oriented access

Common cases should be shorter than structural paths:

```python
obx.children("NTE")
obx.child("NTE", 0)

nte.parent()
```

## Selectors

A richer selector syntax could eventually support:

```text
NTE[parent=OBX]

ORDER/OBSERVATION/NTE

OBX[where 3.1 == "NM"]
```

This should not be required in the initial implementation.

Object APIs and straightforward structural paths should come first.

---

# 8. Resolution modes

The same message can exist at different levels of interpretation.

## Raw

```python
msg = parse(data)
```

Only ER7 parsing occurs.

## Default resolution

```python
resolved = msg.resolve()
```

Applies the positional NTE attachment rule using the native-segment vocabulary. No declared HL7 version or complete schema is required. Optional group grammars constrain scope when selected; they must not silently replace the default attachment rule with standard placement validation.

## Explicit/custom resolution

```python
resolved = msg.resolve(
    grammar=my_vendor_grammar
)
```

Adds caller-supplied groups and attachment rules.

## Standards-informed resolution

Bundled common-event grammars are available as optional interpretations. A future convenience API such as:

```python
resolved = msg.resolve_standard()
```

may select a known structure using MSH message/event metadata, with version metadata only as a hint. Ordinary parsing and manipulation remain available when no bundled structure matches.

Failure to resolve against a standard must never imply that the original message cannot be parsed.

---

# 9. Error tolerance and diagnostics

Parsing problems and structural uncertainty are different categories.

## Parsing

Default parsing should be tolerant.

Recoverable irregularities should preserve the source content and produce diagnostics where appropriate.

## Structural diagnostics

Diagnostics are collected for caller inspection, for example:

```python
resolved.diagnostics
```

Do not automatically print, log, or emit Python warnings. Record a structural issue at its source rather than repeating it for every downstream segment.

Useful resolution diagnostics include an unattached NTE with no eligible anchor in scope, or an ambiguous group boundary under the selected grammar. Unknown Z-segments, skipped vendor segments, first-match singular access, custom grammars, and the default attachment examples in section 4 are not warnings by themselves.

An unfamiliar message is not necessarily ambiguous. Do not flag an otherwise deterministic positional attachment merely because a standard profile would reject the placement. Missing required elements or disallowed placements belong to explicit validation unless they prevent the selected grammar from resolving a boundary.

Callers decide whether to ignore diagnostics, log them, reject selected conditions, or request validation.

## Validation

Validation should be explicit:

```python
validate(msg, profile=...)
```

Early validation support should concentrate on structural concerns:

- expected segments;
- cardinality;
- group boundaries;
- required structural elements.

Full datatype/table/profile validation is later scope.

---

# 10. Mutation

Editing through a structural view modifies the underlying message.

Examples:

```python
patient.segment("PID")["3.1"] = "12345"

obx.add_child(
    "NTE",
    ...
)

order.append(observation)
```

## Insertion semantics

Adding an attached segment should normally place it physically next to its anchor's attachment sequence:

```text
OBX
NTE
NTE
```

Explicit group rules may impose more specific placement requirements.

## Structural invalidation

Ordinary field-value edits leave views valid when the selected grammar does not depend on those values. If a grammar uses field values to resolve structure, changing those inputs also makes the affected view stale.

The following topology changes make all existing structural views of the message stale:

```text
insert segment
remove segment
move segment
change segment type
```

Stale views must not continue returning old relationships or silently re-resolve. Callers explicitly resolve the message again before further structural navigation. Raw segment values and physical serialization remain available independently of resolution. Structural access raises `StaleViewError` until the caller obtains a new view with `msg.resolve(...)`.

Unknown segments and original ordering should otherwise remain untouched.

---

# 11. File and stream processing

File conversion is a primary V2Path use case.

Typical usage should be simple:

```python
for msg in iter_messages(path):
    ...
```

and:

```python
with MessageWriter(output_path) as output:
    for msg in iter_messages(input_path):
        output.write(convert(msg))
```

The first release includes support for:

- one ER7 message per file;
- multiple concatenated MSH messages;
- MLLP framing without requiring a network listener;
- BHS/BTS batch envelopes and FHS/FTS file envelopes;
- file-like streams;
- iterables;
- large inputs without loading everything into memory.

Batch/file envelope information should be preservable without forcing callers interested only in individual messages to manipulate envelopes directly.

HL7 escaping, source encoding, MSH-18, delimiters, and segment terminators receive explicit handling rather than being accidental parser behavior.

Output defaults to preserving the input representation. Provide separate, composable options for:

| Option | Choices |
|---|---|
| Segment terminator | Preserve original, CR, LF, or CRLF |
| Transport framing | Preserve original, bare/unframed, or MLLP |
| Batch/file envelopes | Preserve original, omit explicitly, or generate BHS/BTS with optional FHS/FTS |

MLLP framing uses its own start/end markers and final CR; changing an ER7 segment terminator must not rewrite those framing bytes. Framing support is byte/file processing, not listener lifecycle or ACK handling.

Preserve envelope metadata by default and update affected counts when transformations change their contents. Details for constructing new envelope metadata and writing mixed-format sources remain to be specified. Explicit normalization and encoding conversion may change representation; the default writer must not normalize incidentally.

---

# 12. Transformation model

Transforms remain ordinary Python.

For example:

```python
def convert(msg):
    resolved = msg.resolve(
        grammar=source_grammar
    )

    for observation in resolved.groups("OBSERVATION"):
        ...

    return msg
```

V2Path should not require a transformation DSL.

The first release supports transformations producing:

```text
one message
multiple messages
no message
```

This supports filtering and splitting during file conversions. Aggregation requires state across input messages and is a separate transformation concern.

---

# 13. Proposed package boundaries

An initial package might resemble:

```text
v2path/
  parser.py

  model/
    message.py
    segment.py
    field.py
    encoding.py

  structure/
    grammar.py
    groups.py
    anchors.py
    resolver.py
    diagnostics.py

  path/
    references.py

  io/
    reader.py
    writer.py
    batch.py

  escape.py
```

Optional bundled structure grammars can live under:

```text
v2path/standards/
```

Networking and service-runtime code should stay outside the initial core.

---

# 14. Future lightweight interface runtime

The eventual goal is an always-running service able to perform:

```text
listen
  ↓
parse
  ↓
resolve
  ↓
route
  ↓
transform
  ↓
write/send
```

Potential future inputs and outputs include:

```text
MLLP
files/directories
HTTP
queues
```

Runtime concerns would eventually include:

- listener lifecycle;
- routing;
- concurrency;
- HL7 ACK handling;
- retries;
- persistence;
- replay/failure queues;
- logs;
- metrics;
- service configuration.

These components should use the same V2Path message and transformation model used by file conversions.

The core library should **not** implement these abstractions prematurely.

The important architectural constraint is simply that parsing and transformation are not inherently tied to files.

A later runtime could therefore be a separate distribution such as:

```text
v2path-engine
```

while `v2path` remains independently useful as a Python library.

---

# 15. Relationship to hl7lw

`hl7lw` demonstrates much of the lightweight manipulation experience V2Path should retain.

Its flat message representation is also the architectural limitation V2Path is intended to overcome.

V2Path should preserve convenient ideas where appropriate:

- simple parsing;
- concise references;
- easy mutation;
- straightforward ER7 output.

It should not retain a flat-only structural model for compatibility.

A greenfield implementation with selective compatibility appears cleaner than trying to retrofit all structural semantics into the existing model.

V2Path targets Python 3.11 and newer and uses **GNU GPL version 3 or later**, with SPDX identifier `GPL-3.0-or-later`.

The repository should carry the standard GPLv3 license text and an explicit version-3-or-later grant. The license permits private use and modification, but distribution of covered derivatives or combined applications carries GPL source and licensing obligations. Network use alone does not impose the AGPL network-source requirement.

If third-party source is adapted, verify its license and retain all required attribution and notices. For Apache-2.0 material, its applicable attribution and notice obligations remain relevant; they do not make Apache-2.0 V2Path's project license.

---

# 16. Development milestones

These are implementation stages, not separate release promises. The first release includes the agreed output formats and common bundled grammars as well as the core. The detailed implementation plan remains a separate step.

## Milestone 1: Lossless ER7 core

Implement:

- tolerant parsing;
- message-owned encoding metadata;
- segment/field/component manipulation;
- correct rendering;
- familiar field references;
- escape/unescape handling;
- basic file message iteration.

## Milestone 2: Anchor resolution

Implement:

- declarative anchor rules;
- nearest preceding native non-NTE attachment, skipping unrecognized segments;
- view-bound parent/child access;
- quiet attachment diagnostics;
- insertion/removal of attached segments.

The primary fixtures should exercise NTE after:

```text
PID
PV1
ORC
OBR
OBX
SPM
```

and combinations of those in a single message, including all deterministic examples in section 4. Group boundary isolation becomes mandatory when groups are introduced in milestone 3.

This is the first milestone that materially establishes V2Path's niche.

## Milestone 3: Semantic groups

Implement:

- named groups;
- nesting;
- repetition;
- explicit group boundaries;
- anchor scoping by group;
- structural paths;
- group introspection;
- custom grammars;
- multiple simultaneous views and explicit re-resolution after structural edits.

This milestone should also validate whether names such as:

```text
PATIENT
ORDER
OBSERVATION
```

are sufficiently intuitive in real transformation code.

## Milestone 4: Full file-conversion support

Add:

- multi-message inputs;
- batches;
- file envelopes;
- streaming;
- multiple output messages;
- source-byte preservation and explicit charset conversion;
- preserve/CR/LF/CRLF terminator options;
- MLLP framing independently of batch/file envelopes.

## Milestone 5: Bundled interpretation helpers

Include the optional lightweight structures for ADT A01, SIU S12, ORM O01, ORU R01, and DFT P03. Use 2.5.1 as the initial reference rather than a required caller setting or acceptance gate. The native-segment vocabulary must already be available for milestone 2.

Full standards coverage, automatic grammar selection, expanded structural validation, and profile extension can follow separately. They must not become prerequisites for ordinary manipulation.

## Later: Interface runtime

Only after the core library is mature:

- MLLP listener/client;
- router;
- transformation pipelines;
- destinations;
- durable state;
- retries;
- ACK handling;
- always-running service host.

---

# 17. Testing strategy

Testing should emphasize real structural failure cases, not only textbook-valid messages.

Core fixtures should include:

- repeated OBX/NTE;
- NTE attached to several different segment types in one message;
- multiple orders containing identical segment types;
- nested repeating structures;
- unknown Z-segments between recognized segments;
- missing expected anchors;
- unattached contextual segments;
- malformed but parseable messages;
- different delimiters across sequentially parsed messages;
- parse/render round-trip preservation;
- mutation followed by structural re-resolution;
- large streamed message files;
- custom vendor grammars overriding default attachment behavior;
- all four NTE attachment examples in section 4, without warnings;
- no cross-group attachment and no eligible anchor within a scope;
- simultaneous views with independent relationships and shared field values;
- rejection of stale structural navigation until explicit re-resolution;
- first-match access and separate absent, empty, and explicit-null states;
- no-op text and byte round trips, mixed terminators, escapes, and trailing empties;
- targeted edits preserving unrelated source representation;
- CR/LF/CRLF output, MLLP framing, and batch/file envelope preservation;
- envelope counts after filtering or splitting;
- ordinary manipulation with inaccurate or unsupported version metadata;
- bundled common-event interpretations without mandatory schema validation.

Property-based tests are particularly useful for parser/render invariants.

---

# 18. Current design decisions

1. V2Path is a generic HL7 v2 transformation library, with same-type message edits as a common use case.
2. Python 3.11 is the minimum supported version; the project license is GPL-3.0-or-later.
3. Raw parsing and structural interpretation are separate, and ordinary manipulation is version-independent.
4. Preserve original representation by default, including untouched text or bytes and unknown segments. Normalize only when explicitly requested.
5. NTE attaches to the nearest preceding recognized native non-NTE segment in scope. Skip Z-segments by default; custom rules can make them anchors.
6. Consecutive NTEs share their anchor. Deterministic positional attachments are not warnings or assertions of standard validity.
7. Explicit groups constrain attachment. Never reach into an earlier group; leave NTE unattached when no eligible anchor exists in scope.
8. Physical segment ordering remains authoritative. Semantic groups and physical segments are distinct concepts.
9. Multiple resolved views share raw segments but own their relationships. Independent output variants require copying the message.
10. Structural edits make existing views stale and require explicit re-resolution. Field changes also invalidate interpretations that depend on those values.
11. Singular access takes the first match in scope without warning. Plural and explicit occurrence access remain available.
12. Convenient value access treats absent and empty fields as `""`, while a state API distinguishes absence, emptiness, and explicit HL7 null. Missing segments/groups remain distinct.
13. Diagnostics are available for inspection without automatic logging, printing, or Python warnings. Validation is explicit.
14. HL7 2.5.1 seeds the native-segment vocabulary and reference structures, not an acceptance gate.
15. Bundle optional lightweight interpretations for ADT A01, SIU S12, ORM O01, ORU R01, and DFT P03.
16. The first release includes explicit CR/LF/CRLF output, MLLP framing, and BHS/BTS plus FHS/FTS envelope support. Preserve source formats by default.
17. Semantic group names, introspection, physical navigation, and structural paths coexist.
18. Networking, ACK handling, and a persistent interface runtime remain a separate later layer.

---

# 19. First-release implementation contracts

### Values and addressing

- Fields, components, subcomponents, and repetitions are one-based. Segment/group occurrences and Python lists are zero-based.
- `PID[1]-3[2].1` selects the second PID, second repetition of field 3, component 1.
- Ordinary reads decode supported escapes. Explicit raw access preserves ER7 spelling.
- Ordinary assignment interprets separators below its target level. A field accepts repetitions/components/subcomponents; a component accepts subcomponents. Unescaped higher-level separators raise `AssignmentError` atomically.
- `set_text(...)` escapes delimiters as literal text. Explicit HL7 null uses the exported `NULL` value.
- Missing singular segment/group lookups return `None`; paths through them raise `LookupError`. Missing fields inside present segments read as `""` without materializing fields.
- Plural group and segment lookups inspect direct members by default; `recursive=True` searches descendants. Structural paths always traverse direct scopes.

### Views and mutation

`GroupRule` and `Grammar` provide the boundary API. Standard sequence validation is not part of it. Conflicting static rules are rejected; unresolved runtime boundaries retain physical content with a quiet diagnostic.

Topology changes invalidate all views. Raw values remain usable, while relationship and group navigation raises `StaleViewError` until explicitly resolved again.

Adding NTE places it after the anchor's last attached NTE, or directly after the anchor when none exists. Group movement includes all physical content in its contiguous range, including unknown segments. Cross-message movement is rejected; independent outputs use message copies.

New segments use the preceding terminator style, then a following existing style, then CR. The writer adds a missing separator only when a following record requires it. An untouched standalone unterminated message stays unterminated.

### I/O and encoding

`parse` handles one bare message; `iter_records`, `iter_messages`, `MessageWriter`, and `transform_stream` handle file/stream workflows. Message-only iteration does not reproduce file envelopes; use record iteration or `transform_stream` for preservation of complete files.

Callbacks return one message, an iterable of messages, or `None`. Path destinations are replaced atomically after success; caller-owned output streams can contain partial output after failure. Input is processed incrementally with at most one message and a physical record/read chunk buffered.

Initial byte support covers ASCII, UTF-8, ISO-8859-1, and Windows-1252. Explicit input encoding takes precedence over recognized MSH-18; absent metadata falls back to UTF-8 with reversible preservation of undecodable bytes. Unsupported declared encodings require an override. New values encode strictly. Explicit charset conversion updates hex escapes and MSH-18 and fails when undecodable source data cannot be converted safely.

Delimiter declarations and conflicting charset metadata cannot be changed through ordinary setters. Use explicit output conversion. Opaque escapes that cannot be safely converted cause an error rather than a guess. Explicit delimiter conversion updates message and envelope declarations, data, and matching trailers.

Generated BHS and optional FHS headers require caller-supplied metadata. Trailers use actual output counts. Existing envelopes and empty batches are retained by default; affected message counts update after filtering/splitting. Non-ASCII text envelopes and transcoding of non-ASCII byte envelopes require an explicit source encoding.

### Reference data, packaging, and verification

The native vocabulary and five lightweight presets derive from CRS4 hl7apy's MIT-licensed 2.5.1 data, pinned to revision `a679a01c6b6358242e8d3682868c36ff5134a294`. Retain its notice and provenance. No hl7apy runtime dependency or full datatype/table model is introduced.

The package has no runtime dependencies. Development uses pytest, Hypothesis, and the standard Python build frontend. Distributions include GPLv3 text, the version-3-or-later grant, and third-party notices. CI covers Python 3.11 through 3.14 on Linux and 3.11 on Windows.

Detailed usage and current limits are documented in README.md. Network services, ACK handling, full standards validation, and package publication remain separate work.
