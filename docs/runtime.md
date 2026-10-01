# Network and routed services

The network and service modules ship in `v2path` 0.2.0, require Python 3.11 or newer, and add no runtime dependencies. Import them explicitly. The parsing and editing API remains usable without starting a service.

## MLLP and ACKs

```python
import asyncio
from v2path import parse
from v2path.net import MLLPClient
from v2path.delivery import Retry, RetryPolicy

async def send(message):
    async with MLLPClient('127.0.0.1', 2575) as client:
        return await client.send(message)
```

A client maintains a persistent TCP connection and serializes sends through one lock. Each send freezes its payload and completes its requested ACK sequence before the next send. Network frames contain one bare HL7 message, never file or batch envelopes. Client and server accept an optional `ssl` argument containing a caller-configured `ssl.SSLContext`.

`MLLPServer(host, port, on_message, ack_mode='communication')` calls a sync or async handler with a `Message` and `InboundContext`. Return `Acceptance(completed_future)` after admission or a `DeliveryResult` for an immediate result. Raise `AdmissionRejected` to reject admission. A standalone handler owns its admission policy; `Service` supplies the bounded queue.

| Input header | Response policy |
| --- | --- |
| MSH-15 and MSH-16 empty | Original mode: AA/AE/AR. Route mode chooses admission or application completion timing. |
| Either field populated | Enhanced mode: MSH-15 controls CA/CE/CR, MSH-16 controls AA/AE/AR. An individually empty field means NE. |
| AL | Always send the requested stage. |
| NE | Do not send that stage. |
| ER | Send only on error. |
| SU | Send only on success. |

Enhanced accept and application ACKs use the original connection. A requested application stage waits for all required destinations. The server emits CE on rejected enhanced admission and AE on failed processing. ACK helpers support all six MSA codes; clients recognize negative codes regardless of the local server's choice.

Generated ACKs swap sender/receiver application and facility fields, preserve delimiters, charset, event, processing ID and version, and receive a new control ID and timestamp. An absent processing ID or version uses P or 2.5.1 for the ACK only. MSA-2 echoes the input control ID. ACK messages request no further ACKs. Optional error text uses charset-safe replacement so explanatory text cannot prevent a negative ACK. The original message is unchanged.

Usable headers are required for admission, and a control ID is required whenever an ACK stage is requested. A malformed header/frame closes the connection without fabricating an ACK. `on_error(payload, result)` receives server errors; the server also keeps at most 128 result summaries in `errors`.

Communication mode is the default. Its positive ACK means acceptance into bounded process memory and **does not survive a process crash**. Application mode waits for delivery and forces file publication, even when the batch would otherwise stay open. Neither mode provides end-to-end exactly-once delivery.

## Outcomes and retries

`send()` returns `DeliveryResult` with `outcome`, optional MSA `code`, `detail`, `attempts`, raw `acknowledgements`, and `success`.

| Outcome | Meaning |
| --- | --- |
| confirmed | Every requested stage returned a matching positive ACK. |
| sent | Bytes were sent and no ACK stage was requested. |
| negative_ack | A matching AE, AR, CE, or CR was received. |
| uncertain | Sending began, but a mandatory ACK or transport result was not obtained. |
| unconfirmed | A conditional ACK was absent. Silence is not treated as success. |
| connection_error | The connection could not be established before sending. |
| protocol_error | Invalid framing, correlation, ACK stage, or other protocol violation. |

When an application ACK arrives after a conditionally suppressed accept ACK, it is matched to the application stage. The missing accept stage still makes an otherwise positive result `unconfirmed`. A negative response remains a failed delivery. Connections close after unresolved or invalid ACK sequences so a later response cannot acknowledge the next message.

One attempt is the default. Opt in to retries by MSA code or outcome:

```python
policy = RetryPolicy({'AE': Retry(attempts=3, delay=1),
                      'connection_error': Retry(attempts=2, delay=2)})
```

A matching MSA code takes precedence over an outcome rule. Counts include the first attempt. Retries reuse identical payload bytes, preserve order within that destination, and may duplicate delivery when the previous outcome was uncertain. Configure the policy on a route or its MLLP client, not both. Other destinations continue independently.

## Routes

```python
from v2path.service import (
    Service, Route, MLLPSource, MLLPDestination, RouteDestination,
    FileSource, DirectorySource, FileDestination, DirectoryDestination,
)

route = Route(
    'admissions',
    source=MLLPSource(host='127.0.0.1', port=2575),
    transform=lambda message: message,
    destinations={
        'downstream': MLLPDestination('127.0.0.1', 2576),
        'files': DirectoryDestination('outgoing'),
    },
)
service = Service([route], failed_directory='failed')
```

Use `async with service:` or `await service.start()` and `await service.close()`. Keep your own application running while a listener or watcher should remain active. `await service.wait_sources()` waits for finite file inputs; it also waits for directory watchers when configured. The [loopback example](../examples/mllp_route.py) runs without an external system.

A route has at least one named required destination and either a source or internal admission. A sync or async transform runs once per input and returns one `Message`, an iterable of messages, or `None` to filter successfully. Outputs are validated and frozen before fanout. Each destination receives independent parsed content. No destination can mutate a sibling's output.

`await service.submit('route', message)` returns a `Receipt` after bounded admission. `receipt.dispatched` signals that the transform and enqueue step finished; `receipt.completed` returns the aggregate delivery result. Pass `require_completion=True` to force publication of pending file outputs. Bytes and `Message` inputs are accepted; `Message` inputs retain explicit charset overrides through internal handoffs and file output.

For internal chaining, use `RouteDestination('next')`. It succeeds after the next queue accepts the message. `RouteDestination('next', wait_for='completion')` waits for the downstream required outputs and forces file publication. A later failure after an acceptance-only handoff belongs to the downstream route. For completion handoffs, a downstream failure returns `downstream_failure`; retry and replay stay on that downstream route so successful siblings are never sent again. `allow_internal=False` restricts a route to its configured external source.

Startup rejects missing route references, directed cycles, duplicate file/directory ownership, local listener conflicts, identifiable localhost/directory loops, and archives that are also watched inputs. Endpoint identity uses resolved filesystem paths and common localhost spellings. It does not discover remote aliases, mount aliases, or cycles through systems outside this service. Give one process exclusive ownership of each watched directory and fixed output path.

## Files and directories

`FileSource(path)` reads a finite file once. `DirectorySource(path)` polls nonrecursively for `*.hl7`. Both stream messages through the route and archive successful input files. The default archive is `.v2path-archive` beside the input. Set `archive=` to change it. Explicit `encoding=` overrides unsupported or inaccurate source metadata.

Directory readiness choices:

- `atomic`, the default: producers must finish a temporary file and atomically publish the final name.
- `marker`: process a file after the corresponding `filename.hl7.ready` exists. `marker_suffix` is configurable.
- `stable`: require unchanged size and modification time for `stability` seconds. This is an explicit heuristic and cannot prove a writer is finished.

A watcher atomically claims each input into `.v2path-claimed` before reading it. It ignores hidden names, symlinks, temporary suffixes, completion markers, and internal directories. Failed claimed files move to `.v2path-quarantine`; finite failed files stay in place. Startup quarantines abandoned claims for inspection instead of silently replaying them. Successful inputs are archived only after their required deliveries and file publications succeed.

`DirectoryDestination(path, mode=...)` supports:

| Mode | Publication |
| --- | --- |
| auto | One message per file for a declared chained directory; preserve a file input's grouping for final output; otherwise batch. |
| message | One atomically published file for each transformed output. |
| batch | Rotate on message count, serialized byte count, elapsed time, source end, admission pressure, explicit flush, or shutdown. |
| preserve | One output artifact for an input file, including split/filter results. Inputs without a file group use batch mode. |

Preserved input groups spool both payloads and member metadata to disk. Their per-message receipt can complete with `accepted` after staging to release memory; the source still waits for group publication before archival. This avoids keeping a whole large file's receipts in memory or splitting it when a sibling destination is slow. Explicit application completion can force an earlier publication. No empty timer batches are created.

`FileDestination(path)` publishes a fixed file and therefore requires a finite `FileSource`. Use rotating directory outputs for indefinite traffic. Input envelopes are not copied by routed message processing. File outputs concatenate ER7 by default; supply `batch_header` and optional `file_header` to generate BHS/BTS and FHS/FTS. Business header metadata comes from the caller. Existing `transform_stream()` remains the API for envelope-preserving whole-file conversion.

File destinations accept `framing='bare'` or `'mllp'`, `terminator=None`/CR/LF/CRLF, and `encoding=`. Generated envelopes require bare file framing. Network MLLP always frames one message. Declared chained directories enforce atomic readiness and compatible filename globs; they can carry individual messages or complete batches.

Publication writes a temporary file on the destination filesystem, flushes and closes it, then uses a no-clobber hard link to publish its final name. The filesystem must support hard links. An existing target is a delivery failure, never an overwrite. A leftover internal temp after successful publication is cleanup work, not a reason to resend.

Filename templates accept plain tokens `route`, `destination`, `timestamp`, `id`, `batch_id`, `sequence`, and `source_stem`. Formatting directives and path separators are rejected. Timestamps use UTC and IDs are unique. Publication retries keep the same bytes and final name.

Defaults:

- Message: `{route}-{timestamp}-{id}.hl7`
- Batch: `{route}-{timestamp}-{batch_id}.hl7`
- Preserved group: `{source_stem}-{id}.hl7`

## Failure retention and explicit replay

`failed_directory` is mandatory. Each failure is an atomically published directory with `record.json` and exact `payload.hl7` bytes. Records identify the route, destination, source, timestamps, attempts, outcome and ACKs. Failed batches have one shared artifact plus `members.jsonl`, including original member payloads. Transform failures retain the original input. Per-destination failure tracking leaves successful sibling deliveries alone.

```python
records = service.failures.records()
result = await service.replay(records[0].name)
```

Replay is explicit; startup never automatically resends failures. A replayed artifact is published once, regardless of its member count. Successful replay writes a `replayed` marker; repeating that record ID is rejected. A failed message replay leaves the same actionable record. A transform replay that creates newer failures marks its original record superseded. Destination configuration must still match its recorded target. Input failures and incomplete artifacts require inspection and explicit source/member recovery. They cannot be blindly replayed as complete batches.

If failure storage cannot be written, the service retains pending work and pauses admission. Inspect `service.errors`, repair storage, and call `service.resume()` to retry the retained writes. Errors contain no automatic logs; payloads remain in the configured storage. A drain timeout raises `TimeoutError` without discarding admitted work. Keep the service object and event loop alive, repair the blocker if needed, and call `close()` again. Terminating the process still loses in-memory work.

## Limits and scope

| Setting | Default |
| --- | --- |
| Route admitted input | 128 items, 64 MiB retained input bytes |
| Transform output per input | 1000 messages, 64 MiB |
| MLLP frame | 16 MiB |
| Inbound connections per listener | 32 |
| Connect timeout | 10 seconds |
| Frame assembly, send, each ACK stage | 30 seconds each |
| Idle connection | 300 seconds |
| Graceful drain | 30 seconds |
| Directory poll / stability window | 1 / 5 seconds |
| Final batch rotation | 1000 messages / 16 MiB / 60 seconds |

Route queues, destination queues, and pending batch receipts are bounded. A batch may publish below its configured count under admission pressure. One message can cross a batch byte threshold; it is never split. The route input-byte limit is not a cap on the entire process: transforms, parser buffers, destination queues, and per-connection frame buffers consume additional memory. File spools and failure storage consume disk space. Filesystem operations use ordinary synchronous stdlib calls; slow filesystems can delay the event loop. The runtime is intended for local filesystems with monitored capacity.

HTTP delivery, durable broker queues, automatic startup replay, an OS service installer, a management API, and full HL7 standards validation are outside this release. Application code owns process supervision and TLS configuration. The destination API can be extended with HTTP request/header configuration when that transport is implemented.
