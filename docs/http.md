# Outbound HTTP delivery

Version 0.3.0 adds HTTP and HTTPS destinations to the same dependency-free package. The client sends one message per request, defaults to POST, and treats HTTP 2xx as success. It does not provide an incoming HTTP listener or manage OAuth tokens.

## Standalone client

```python
from v2path.http import HTTPClient

async def deliver(message):
    async with HTTPClient('https://example.test/messages') as client:
        result = await client.send(message)
        return result
```

`send()` accepts a `Message` or bare HL7 bytes. The default body is the message's exact serialized bytes, without MLLP framing, with `Content-Type: application/hl7-v2`. It preserves encoding and line endings. Configure a content-type charset explicitly if the receiving endpoint requires it.

The client reuses HTTP/1.1 connections and permits one request at a time. HTTP and HTTPS are supported; HTTPS verifies certificates and hostnames by default. Supply `ssl=your_ssl_context` for a private CA or client certificate. URL credentials and fragments are rejected; percent-encode non-ASCII URL components. The HTTP method is configurable. CONNECT and TRACE are not delivery methods.

No redirects, automatic cookies, environment proxy discovery, or implicit retries occur. A stale persistent connection produces a failed/uncertain result rather than an automatic resend.

## Encoding and headers

```python
import json
from v2path.http import HTTPClient

async def encode(message):
    return json.dumps({'hl7': message.to_text()}).encode('utf-8')

async def headers(message, body, attempt):
    token = await application_token_provider()
    return {'Authorization': f'Bearer {token}'}

client = HTTPClient(
    'https://example.test/messages',
    headers={'Content-Type': 'application/json'},
    encoder=encode,
    header_provider=headers,
)
```

Both callbacks may be synchronous or asynchronous. The encoder receives an independent message and must return `bytes`. It runs once per logical delivery. Its body is frozen before sending and reused unchanged for retries and replay. A custom representation requires an appropriate configured content type.

The header provider receives an independent original message, the frozen body, and the one-based attempt number. It runs before each attempt, including replay, so application code can obtain fresh credentials or sign the exact body. Header names merge case-insensitively, with provider values overriding static configuration. Attempts restart at one for an explicit replay.

The application owns authentication acquisition and refresh. Static Authorization/API-key headers and dynamic credentials are supported. The library does not invent idempotency keys from MSH-10. If the receiving system supports idempotency, the application should supply a stable key appropriate to its delivery identity.

Host, Content-Length, transfer framing, and connection-management headers are controlled by the client. Conflicting headers, invalid names, and control characters in values are rejected before sending. Header values must be representable in Latin-1. No credential-bearing request headers are written to failure records.

For callers that need explicit preparation, `await client.prepare(message)` returns an immutable `HTTPRequest` containing URL, method, body, original message bytes, and encoding. `await client.send_prepared(request)` sends it without encoding again and checks that its target and method match the client.

## Response policies

`DeliveryResult.http_response` is an optional `HTTPResponse` snapshot with `status`, `headers` as a tuple of name/value pairs, and `body` as bytes. Repeated header names are retained. Existing delivery result fields and MLLP behavior are unchanged. Route receipts aggregate required deliveries; they do not retain individual successful response snapshots. Use the standalone client or a response evaluator when application code needs a successful response body.

Choose the client's `response` option:

- `'status'`, the default: 2xx succeeds. `accepted_statuses={200, 202, 409}` replaces that set when an endpoint uses another success convention.
- `'hl7_ack'`: require an accepted HTTP status and one bare HL7 ACK whose MSA-2 matches the original MSH-10. AA or CA succeeds; AE/AR/CE/CR fails. A missing control ID is rejected before sending. This mode does not negotiate MLLP's two-stage ACK exchange from MSH-15/16.
- A sync/async evaluator: receive `(response, original_message)` and return `DeliveryResult`. It controls business success after a complete, valid HTTP response, including endpoint-specific handling of non-2xx statuses. Transport failures bypass the evaluator.

```python
from v2path.delivery import DeliveryResult

async def evaluate(response, message):
    if response.status == 409 and response.body == b'already processed':
        return DeliveryResult('confirmed')
    return DeliveryResult('confirmed' if response.status == 200 else 'http_error')
```

Informational responses such as 100, 102, and 103 are consumed before the final response. Protocol upgrades, ambiguous response framing, and oversized responses fail and close the connection.

| Outcome | Meaning |
| --- | --- |
| confirmed | The selected response policy accepted the delivery. |
| http_error | HTTP status was not accepted. |
| negative_ack | A correlated negative HL7 ACK was received. |
| request_error | Encoding, request limits, header creation, or validation failed before sending. |
| response_error | ACK parsing/correlation or the custom evaluator failed. |
| connection_error | Connection establishment failed before sending. |
| uncertain | Sending began, but the transport did not produce a complete response. |
| protocol_error | HTTP framing, protocol switching, or response limits were violated. |

Callback failure details contain the exception type, not exception text that might expose credentials. Inspect response snapshots and application callbacks for diagnosis. Incomplete responses may have only status/headers available.

## Retry and route integration

One attempt remains the default. Retry rules accept HTTP status keys alongside existing MSA and outcome keys:

```python
from v2path.delivery import Retry, RetryPolicy
from v2path.service import HTTPDestination, Route, Service

route = Route(
    'adt',
    destinations={'api': HTTPDestination(
        'https://example.test/messages',
        headers={'Content-Type': 'application/hl7-v2'},
        options={'response_timeout': 20},
    )},
    retry=RetryPolicy({'http:503': Retry(3, 2),
                       'connection_error': Retry(2, 1)}),
)
service = Service([route], failed_directory='failed')
```

MSA-code rules take precedence over HTTP-status rules, which take precedence over outcome rules. A successful result is never retried. Configure retry either on the route or in the destination's client options, not both. `Retry-After` is available in response headers but does not create a retry or override the configured delay.

Each destination preserves delivery order through retries; sibling destinations operate independently. HTTP participates in normal route completion, file archival, application ACK timing, bounded admission, and graceful service drain. An HTTP 202 success means the remote server accepted the request, not that its later asynchronous work finished.

## Failure records and replay

A failed prepared HTTP delivery stores the original `payload.hl7`, exact encoded `request.body`, target URL/method, attempts, outcome, and response diagnostics in one atomic failure directory. Response diagnostics retain at most 64 KiB of body and only these headers: Content-Type, Retry-After, Date, X-Request-ID. The response body and URL can contain sensitive application data; these are local failure artifacts, not logs. Request headers and response cookies are not persisted.

`await service.replay(record_id)` sends the saved body without rerunning the transform or encoder. The current destination URL and method must match the saved request. Static headers come from current configuration; the current header provider supplies fresh credentials. Configure the same content type and other representation headers when reopening a service for replay.

Only the failed destination is replayed. Successful siblings remain untouched. A failed replay keeps its existing actionable record; a successful replay marks it complete. Old non-HTTP failure records remain readable. Encoding failures have no prepared body and are recorded as `http_prepare`; repair the encoder and explicitly resubmit the retained original message instead of replaying a body that never existed.

If failure storage is unavailable, admission pauses and work remains retained. Repair storage and call `service.resume()` as described in the [runtime guide](runtime.md).

## Timeouts and lifecycle

Defaults are 10 seconds for connection establishment, 30 seconds each for request sending and the complete response phase, and 16 MiB each for request and response bodies. Configure `connect_timeout`, `send_timeout`, `response_timeout`, `max_request_bytes`, and `max_response_bytes`. The response deadline covers headers, informational responses, and body together; a slow trickle does not reset it.

The stdlib blocking HTTP operations run in worker threads, with at most one outstanding exchange per client. Cancellation or `close()` interrupts the socket and waits for the worker before releasing connection ownership, preventing a late send from overlapping another attempt. Closing also interrupts retry delays. The default asyncio executor bounds concurrent worker threads; route queues bound queued deliveries.

Python cannot interrupt an operating-system DNS resolver already executing `getaddrinfo`. If DNS stalls, cancellation/close can wait for it to return. An expired deadline or cancellation prevents further socket activity afterward. User callbacks run outside network I/O deadlines; application code owns their timeouts. Imports start no sockets, threads, or event loops.

Run `python examples/http_route.py` after installation for a self-contained local HTTP example. Incoming HTTP, OAuth token management, HTTP/2, proxy support, and automatic startup replay are outside this release.
