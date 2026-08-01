# OWASP security disposition

Baseline date: 2026-08-01. Standards: [OWASP Top 10:2025](https://owasp.org/Top10/2025/en/), [OWASP API Security Top 10:2023](https://owasp.org/API-Security/editions/2023/en/0x11-t10/), and [OWASP Top 10 for LLM Applications:2025](https://genai.owasp.org/llm-top-10/).

`PASS` below means a deterministic repository gate covers the runtime-owned control. `SYSTEM` means the risk is owned by One Brain, model/data governance, deployment infrastructure or the product ingress and must not be represented as runtime-tested.

## Web and API risks

| Risk | Status | Runtime control/evidence |
|---|---|---|
| A01 Broken Access Control / API1, API3, API5 | PASS | Signed tenant/task contract; forbidden authority scopes; cross-tenant completion test |
| A02 Security Misconfiguration / API8 | PASS | Managed worker opens no public listener; strict Pydantic schemas; fail-closed defaults |
| A03 Software Supply Chain Failures | PASS | Hash-bearing `uv.lock`, npm registry integrity, SHA-pinned Actions, dependency gate |
| A04 Cryptographic Failures / API2 | PASS | Ed25519 signatures; tamper/expiry tests; HTTPS-only outbound validation |
| A05 Injection | PASS (boundary) | Objective cannot self-authorize effects; no shell in quick command/editor/catalog paths; blocking SAST profile |
| A06 Insecure Design | PASS (boundary) | One Brain / non-sovereign runtime contract and Effect Gate proposal model |
| A07 Authentication Failures | PASS (service identity) | Issuer/audience/key/signature/time validation; end-user authentication remains SYSTEM |
| A08 Software or Data Integrity Failures | PASS | Signed canonical payload, replay protection and lock integrity gates |
| A09 Logging and Alerting Failures | SYSTEM | Trace identifiers and completion evidence are emitted; alert routing belongs to the owning platform |
| A10 Mishandling Exceptional Conditions | PASS (boundary) | Malformed/expired/tampered commands fail closed; subprocess worker returns non-zero |
| API4 Unrestricted Resource Consumption | PASS (contract) | Shared iteration, spawn, concurrency, token and deadline limits |
| API6 Unrestricted Business Flows | SYSTEM | Business-flow policy belongs to One Brain/product ingress |
| API7 SSRF | PASS | Scheme, credentials, local/private/link-local address rejection tests |
| API9 Inventory Management | PASS (runtime) | Versioned command/effect/completion schemas; no independent public endpoint |
| API10 Unsafe Consumption of APIs | PASS (boundary) | External URLs validated and all network operations remain proposals |

## LLM risks

| Risk | Status | Runtime control/evidence |
|---|---|---|
| LLM01 Prompt Injection | PASS (authority) | Adversarial objective cannot widen tools or authorize network effects |
| LLM02 Sensitive Information Disclosure | PASS (boundary) | Secret redaction plus repository secret gate |
| LLM03 Supply Chain | PASS | Dependency locks, immutable Actions and quarantined update process |
| LLM04 Data and Model Poisoning | SYSTEM | Training, model and Canon promotion are outside runtime authority |
| LLM05 Improper Output Handling | PASS (effects) | Output cannot directly cause an external effect; arguments are digest-bound proposals |
| LLM06 Excessive Agency | PASS | Non-sovereign runtime, explicit tool allowlist and Effect Gate |
| LLM07 System Prompt Leakage | SYSTEM | System-prompt construction and disclosure tests belong to One Brain |
| LLM08 Vector and Embedding Weaknesses | SYSTEM | Retrieval, tenant indexes and memory promotion belong to MemoryBus/One Brain |
| LLM09 Misinformation | SYSTEM | Verifier/evidence fields are mandatory contracts; semantic truth evaluation is upstream |
| LLM10 Unbounded Consumption | PASS (contract) | Shared bounded reasoning envelope, spawn depth/concurrency and expiry tests |

The smoke gate executes only the `PASS` controls. It never converts a `SYSTEM` responsibility into a passing runtime claim.
