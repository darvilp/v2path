# V2Path Design

**Status:** Initial design snapshot  
**Project:** `v2path`  
**Primary language:** Python  
**Primary use case:** HL7 v2 file-based parsing, transformation, and generation  
**Long-term direction:** Optional lightweight always-running interface runtime

## 1. Purpose

V2Path is a lightweight HL7 v2 parsing and transformation library aimed at the gap between simple ER7 manipulation libraries such as `hl7lw` and schema-heavy libraries such as `hl7apy`.

The desired experience is:

- convenient and tolerant for ordinary message manipulation;
- aware of repeating and nested message structure when structure matters;
- usable without loading or satisfying a complete HL7 schema;
- lossless enough to preserve unknown, vendor-specific, or structurally unusual content;
- particularly well suited to file-based conversions;
- reusable later inside an always-running listener/router/transform/writer service.

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

Standard HL7 structure definitions can eventually be supplied by the library, but they are helpers rather than prerequisites.

The library should remain useful against imperfect real-world HL7.

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
  segment_terminator
```

Encoding state belongs to each message and should be immutable.

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

Anchor attachment is V2Path's default structural mechanism.

An **anchor** is a segment capable of owning or contextualizing following segments.

An attachment rule states that a segment should attach to the nearest preceding eligible anchor that remains active in its structural scope.

Conceptually:

```python
attach("NTE").to_nearest(
    "OBX",
    "OBR",
    "PID",
    "PV1",
    "SPM",
)
```

Given:

```text
PID
NTE
NTE
ORC
OBR
NTE
OBX
NTE
NTE
OBX
NTE
```

the inferred relationships become:

```text
PID
  NTE
  NTE

OBR
  NTE

OBX
  NTE
  NTE

OBX
  NTE
```

The NTE remains an ordinary physical segment. Attachment adds a structural relationship.

## Anchor scope

The rule must **not** mean:

> Find the nearest matching segment anywhere before this segment.

Instead:

> Find the nearest eligible anchor still active inside the current structural scope.

For example:

```text
ORDER 1
  OBR
  OBX
  NTE

ORDER 2
  OBR
  NTE
```

The last NTE must not attach to the OBX from ORDER 1.

Explicit groups therefore constrain anchor resolution when groups are available.

## Context-stack implementation

Internally, the resolver can maintain active contexts:

```text
read OBR
    current order anchor = OBR

read OBX
    current observation anchor = OBX

read NTE
    attach -> current OBX

read OBX
    replace current observation anchor

read NTE
    attach -> new OBX
```

A context stack is an implementation technique rather than the user-facing abstraction.

The initial implementation should favor a conceptually simple two-stage process:

```text
raw message
    ↓
group resolution
    ↓
anchor resolution within group scopes
    ↓
resolved message
```

This can later be optimized into a single pass if profiling shows a meaningful reason.

## Anchor API

Anchor relationships should be directly accessible.

Possible API:

```python
obx.children("NTE")
obr.children("NTE")

nte.parent()
segment.attachments()
```

Relative physical navigation should also be available:

```python
obx.next("NTE")
obr.until("OBX")
segment.previous("ORC")
```

These operations should not require callers to write a path expression.

---

# 5. Explicit structural groups

Anchors intentionally do not solve every structure.

V2Path therefore supports explicit nested and repeating groups.

A simple rule might look like:

```python
group(
    "ORDER",
    start="ORC",
    repeat=True,
)

group(
    "OBSERVATION",
    start="OBX",
    parent="ORDER",
    repeat=True,
)
```

A more complex structure could use a sequence grammar:

```python
group("ORDER", [
    optional("ORC"),
    required("OBR"),

    many(
        group("OBSERVATION", [
            required("OBX"),
            many("NTE"),
        ])
    ),
])
```

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

Useful API properties could include:

```python
group.name
group.anchor
group.start_segment
group.segment_types()

segment.group()

msg.groups()
msg.structure()
```

For example:

```python
print(msg.structure())
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
msg.group("PID")
```

as:

```python
msg.group("PATIENT")
```

That becomes ambiguous as structures become more complicated.

Instead, the relationship should be discoverable:

```python
msg.groups_containing("PID")
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
msg.group("ORDER", 1) \
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

Applies V2Path's lightweight default anchor behavior and any appropriate default structural information.

## Explicit/custom resolution

```python
resolved = msg.resolve(
    grammar=my_vendor_grammar
)
```

Adds caller-supplied groups and attachment rules.

## Standards-informed resolution

Later:

```python
resolved = msg.resolve_standard()
```

may select a known structure using MSH version/message/event metadata.

Failure to resolve against a standard must never imply that the original message cannot be parsed.

---

# 9. Error tolerance and diagnostics

Parsing problems and structural uncertainty are different categories.

## Parsing

Default parsing should be tolerant.

Recoverable irregularities should preserve the source content and produce diagnostics where appropriate.

## Structural diagnostics

Resolution could produce conditions such as:

```text
unattached NTE

ambiguous group boundary

unexpected segment inside ORDER

missing expected OBR

unknown segment ZXY
```

These should normally be available as:

```python
resolved.diagnostics
```

rather than immediately terminating processing.

Callers can decide whether to:

- ignore them;
- log them;
- reject selected conditions;
- invoke stricter validation.

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

Changing field values normally does not affect grouping.

Topology changes may:

```text
insert segment
remove segment
move segment
change segment type
```

V2Path may invalidate the affected structural indexes and lazily re-resolve them.

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

The I/O layer should eventually understand:

- one ER7 message per file;
- multiple concatenated MSH messages;
- FHS/BHS/BTS/FTS envelopes;
- file-like streams;
- iterables;
- large inputs without loading everything into memory.

Batch/file envelope information should be preservable without forcing callers interested only in individual messages to manipulate envelopes directly.

HL7 escaping, source encoding, MSH-18, delimiters, and segment terminators should receive explicit handling rather than being accidental parser behavior.

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

A transformation should eventually be able to produce:

```text
one message
multiple messages
no message
```

This supports filtering, splitting, and aggregation during file conversions.

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

Optional standard grammars could later live under:

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

If source code is directly adapted from `hl7lw`, the previously identified Apache-2.0 attribution and notice requirements apply.

---

# 16. Development milestones

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
- nearest-active-anchor resolution;
- parent/child access;
- attachment diagnostics;
- insertion/removal of attached segments.

The primary fixtures should exercise NTE after:

```text
PID
PV1
OBR
OBX
SPM
```

and combinations of those in a single message.

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
- custom grammars.

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
- stronger charset handling.

## Milestone 5: Optional standards support

Add:

- bundled standard structure grammars;
- automatic message/version selection;
- structural validation;
- custom profile extension.

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
- custom vendor grammars overriding default attachment behavior.

Property-based tests are particularly useful for parser/render invariants.

---

# 18. Current design decisions

The project currently assumes:

1. V2Path is primarily a lightweight HL7 v2 transformation library.
2. Raw parsing and structural resolution are separate layers.
3. Anchor attachment is the default structural mechanism.
4. Explicit grouping supplements anchors for complicated structures.
5. Groups constrain anchor scope rather than replacing anchors.
6. Raw physical segment ordering remains authoritative.
7. Segments and semantic groups are distinct concepts.
8. Semantic group names such as `PATIENT`, `ORDER`, and `OBSERVATION` are preferred over segment-code group names.
9. The API compensates for semantic-name opacity with strong introspection and representations such as `PATIENT<PID>`.
10. Relative anchor operations and full structural paths coexist.
11. File and stream conversions are first-class near-term features.
12. Standards/schema support is optional.
13. The eventual listener/router/transform/writer service is a separate later layer.

---

# 19. Remaining design questions

The core architecture is reasonably settled, but implementation planning should resolve several details.

### Grammar syntax

Determine the cleanest declarative syntax for:

- anchors;
- group boundaries;
- repetition;
- group nesting;
- explicit overrides.

### Built-in grammar depth

Decide how much V2Path should understand by default.

Possible spectrum:

```text
generic anchor rules only
        ↓
common lightweight structural rules
        ↓
full per-message HL7 group definitions
```

The likely answer is the middle option with optional full definitions.

### Group naming

Semantic names are the current recommendation.

The first prototype should specifically evaluate whether code such as:

```python
msg.group("PATIENT")
msg.group("ORDER")
msg.groups("OBSERVATION")
```

feels natural after short exposure.

If it does, retain it.

If it proves too opaque, improve discoverability before considering segment-code aliases. The distinction between a semantic container and its physical anchor segment should remain intact regardless of syntax.

### Mutation placement

Define deterministic rules for inserting:

- attached children;
- new anchors;
- new groups;
- unknown/custom segments.

### Resolution lifecycle

Decide whether:

```python
msg.resolve()
```

returns a persistent view, caches indexes on the message, or both.

### Standard definitions

Decide whether standard structural grammars are:

- hand-maintained;
- generated from machine-readable HL7 definitions;
- a combination of generated definitions plus curated overrides.

These questions can be resolved incrementally without changing V2Path's central architecture.