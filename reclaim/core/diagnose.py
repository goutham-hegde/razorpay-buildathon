"""Root-cause diagnosis: messy issuer text in, a `RootCause` out.

This is the only place in the project where a language model is called, and the README says
so explicitly. Retry timing is a policy, budgets are arithmetic, and anything that moves
money is plain code behind a gate. The model does one job: read free text that no two
issuers write the same way, and name what actually went wrong.

WHY THIS IS HARD, AND WHY IT IS WORTH A MODEL
---------------------------------------------
Three pairs of causes are deliberately near-indistinguishable in the generated text, because
they are near-indistinguishable in reality:

    ambiguous_debited   vs  issuer_technical_decline    both read as a timeout
    mandate_revoked     vs  instrument_invalid          both read as "not valid"
    psp_routing_failure vs  issuer_technical_decline    both read as a gateway error

The first pair is the expensive one. `ambiguous_debited` means the customer may already have
been debited; retrying *succeeds*, and that success is a duplicate charge. The only honest
signal separating it from a plain timeout is that a bank reference tends to come back when
money actually moved - a tendency, not a guarantee. A regex cannot weigh a tendency. That is
the gap this step exists to measure.

THE MODEL RUNS ONCE PER BATCH, EVER
-----------------------------------
Output is written to `data/<batch>/diagnoses.jsonl` and that file is **committed**. The eval
harness reads it; it never calls a model. This is not a cost optimisation - it is what makes
the reproducibility claim true. Someone cloning this repo has no API key of any kind, and
every number in the README still has to reproduce for them.

The run is resumable for the same reason it is cached: free-tier rate limits mean a
1,200-case pass gets interrupted, and losing 700 completed calls to a 429 on the 701st would
be its own kind of expensive.

SEALED. This module may not import the simulated world - see `tests/test_seal.py`, whose
string check fires on the module name appearing anywhere in `core/`, prose included. It is
scored by `reclaim.eval.confusion`, which is allowed to read both sides. The diagnoser never
sees the answer it is being graded on.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Protocol

from reclaim.domain import Case, ObservedError, RootCause

# ---------------------------------------------------------------------------
# The prediction
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Diagnosis:
    """One case's predicted root cause, and enough provenance to audit it."""

    case_id: str
    root_cause: RootCause
    confidence: float
    rationale: str
    provider: str
    model: str

    def to_json(self) -> str:
        d = asdict(self)
        d["root_cause"] = str(self.root_cause)
        return json.dumps(d, separators=(",", ":"))

    @staticmethod
    def from_dict(raw: dict) -> "Diagnosis":
        return Diagnosis(
            case_id=raw["case_id"],
            root_cause=RootCause(raw["root_cause"]),
            confidence=float(raw.get("confidence", 0.0)),
            rationale=raw.get("rationale", ""),
            provider=raw.get("provider", "unknown"),
            model=raw.get("model", "unknown"),
        )


class Diagnoser(Protocol):
    """Anything that can name a root cause. The policy depends on this, not on a vendor."""

    name: str
    model: str

    def diagnose(self, case: Case) -> Diagnosis: ...


# ---------------------------------------------------------------------------
# What the diagnoser is allowed to see
# ---------------------------------------------------------------------------


def observable(case: Case) -> dict:
    """The exact payload handed to a diagnoser. Nothing here comes from ground truth.

    Kept as one function so that "what the model saw" is a single reviewable place, and so
    the stub and the model are provably given identical inputs.
    """
    attempt = case.attempts[-1] if case.attempts else None
    err: ObservedError | None = attempt.error if attempt else None
    return {
        "rail": str(case.rail),
        "kind": case.kind,
        "issuer": case.issuer,
        "psp": case.psp,
        "amount_paise": case.amount_paise,
        "attempt_no": attempt.attempt_no if attempt else 0,
        "error_code": err.code if err else None,
        "error_source": str(err.source) if err else None,
        "error_step": str(err.step) if err else None,
        "error_reason": err.reason if err else None,
        "error_description": err.description if err else None,
        # The single most load-bearing field, and the reason this is not a regex problem:
        # a bank reference is *more likely* when money actually moved. A tendency, not proof.
        "bank_reference": err.bank_reference if err else None,
    }


# ---------------------------------------------------------------------------
# The offline stub — an arm, not a fallback
# ---------------------------------------------------------------------------

#: Ordered keyword rules. First match wins, so the specific patterns come first.
#: This is what a competent afternoon of string matching gets you, and the point of
#: running it as its own arm is to find out how much more the model is worth than that.
_STUB_RULES: tuple[tuple[str, RootCause, float], ...] = (
    (r"insufficient|bal(ance)? low|not enough|nsf|acct bal", RootCause.INSUFFICIENT_FUNDS, 0.80),
    (r"limit|exceed|per txn cap|daily cap", RootCause.LIMIT_EXCEEDED, 0.72),
    (r"expired|invalid card|blocked|closed|do not honour|card not", RootCause.INSTRUMENT_INVALID, 0.68),
    (r"revoked|cancelled by|mandate not|umn not found|de-?registered", RootCause.MANDATE_REVOKED, 0.66),
    (r"risk|fraud|suspicious|velocity|declined by rule", RootCause.RISK_DECLINED, 0.70),
    (r"abandon|cancelled at|not approved by payer|expired.*collect|user dropped", RootCause.AUTH_ABANDONED, 0.66),
    (r"gateway|route|upstream|psp|switch", RootCause.PSP_ROUTING_FAILURE, 0.55),
    (r"timeout|timed out|no response|deemed|pending settlement|unknown", RootCause.ISSUER_TECHNICAL_DECLINE, 0.50),
)


class StubDiagnoser:
    """Deterministic keyword matching. No network, no key, no model.

    This exists to be beaten. It runs as its own arm so the results table can separate two
    things that otherwise get credited to the same place: how much of the agent's lift comes
    from the *diagnosis*, and how much comes from the policy wrapped around it. An agent that
    beats naive retry by a mile but only beats this by a hair has a good policy and an
    expensive classifier.

    It also means the test suite, CI, and an offline demo never need a key.
    """

    name = "stub"
    model = "keyword-rules"

    def diagnose(self, case: Case) -> Diagnosis:
        obs = observable(case)
        haystack = " ".join(
            str(obs.get(k) or "")
            for k in ("error_description", "error_reason", "error_code", "error_source")
        ).lower()

        for pattern, cause, confidence in _STUB_RULES:
            if re.search(pattern, haystack):
                # Deliberately does NOT special-case the bank reference. Weighing a tendency
                # is precisely what a keyword rule cannot do, and pretending otherwise here
                # would flatter the stub and understate what the model contributes.
                return Diagnosis(
                    case.id, cause, confidence, f"matched /{pattern}/", self.name, self.model
                )

        return Diagnosis(
            case.id,
            RootCause.ISSUER_TECHNICAL_DECLINE,
            0.25,
            "no rule matched; fell back to the modal cause",
            self.name,
            self.model,
        )


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------

_TAXONOMY = """\
issuer_technical_decline  The issuer or switch failed to respond, or returned a transient
                          system error. Nothing is wrong with the customer, the money or the
                          instrument. A retry in minutes usually works.
insufficient_funds        The account did not have the money. The instrument is fine and the
                          customer is willing; only the timing is wrong.
auth_abandoned            Nobody completed the step. The customer left an OTP screen, ignored
                          a collect request, or never opened the app. Nothing was declined.
instrument_invalid        The card or account cannot be charged at all - expired, blocked,
                          closed. No retry will ever succeed.
limit_exceeded            A per-transaction or daily cap was hit. Retryable later or lower.
mandate_revoked           The stored authorisation no longer exists; the customer removed it.
                          Distinct from instrument_invalid: the instrument may be perfectly
                          fine, it is the *permission* that is gone.
risk_declined             A fraud or risk rule fired. Retrying argues with the rule.
psp_routing_failure       Our own gateway or route to the bank broke, not the bank itself.
                          A different PSP would work right now.
ambiguous_debited         No clean answer came back and the customer MAY ALREADY HAVE BEEN
                          DEBITED. Deemed-approved, pending-settlement, reconciliation-
                          required, or a timeout that still returned a bank reference.
                          Retrying this takes the money a second time."""

_SYSTEM_PROMPT = f"""\
You classify failed Indian digital payments by root cause, so that a recovery system can
choose an action. Your output decides whether money is retried, when, and whether a customer
is contacted, so a confident wrong answer is worse than an honest uncertain one.

Return exactly one of these nine causes:

{_TAXONOMY}

HOW TO WEIGH THE EVIDENCE

Issuer error text is inconsistent, abbreviated and often misleading. `error_reason` and
`error_code` are frequently generic (`payment_failed`, `BAD_REQUEST_ERROR`) and carry little
signal. The free-text `error_description` carries most of it.

Three distinctions are genuinely hard, and matter more than the rest combined:

1. ambiguous_debited vs issuer_technical_decline. Both look like a timeout. The strongest
   available signal is `bank_reference`: banks tend to return one when money actually moved.
   It is a tendency, not proof - a plain timeout sometimes carries one, and a real debit
   sometimes does not. Weigh it, do not treat it as decisive. Language about settlement,
   reconciliation, "deemed", or an unknown/unconfirmed final state also points here.
   Getting this wrong in the direction of "technical decline" causes a DOUBLE CHARGE.

2. mandate_revoked vs instrument_invalid. Both read as "not valid". Ask whether the text is
   about the *instrument* (expired, blocked, closed, card-level) or about the *permission*
   (mandate, UMN, registration, subscription, revoked, cancelled by user). A one-time
   payment on a card has no mandate to revoke; a stored-mandate rail has no OTP screen.

3. psp_routing_failure vs issuer_technical_decline. Both read as a gateway error. Language
   naming our own switch, route, or gateway points to routing; language naming the issuer or
   the bank points to the issuer.

Use `rail` and `kind` as constraints. A recurring debit against a stored mandate has no human
present, so it cannot be auth_abandoned. A one-time payment has no mandate to revoke.

Set `confidence` to your actual belief, between 0 and 1. On the confusion pairs above, a
value near 0.5 is the correct answer when the text genuinely does not separate them; the
recovery policy treats low confidence as a reason to act cautiously, which is exactly what
you want when you cannot tell."""

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "root_cause": {"type": "string", "enum": [str(c) for c in RootCause]},
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
    },
    "required": ["root_cause", "confidence", "rationale"],
}


class QuotaExhausted(RuntimeError):
    """A free-tier quota is spent. Distinct from a transient error: waiting will not help.

    Raised rather than swallowed so a run stops loudly with what it has, instead of burning
    an hour of retries against a daily cap. The cache is already written, so resuming after
    the quota resets costs nothing.
    """


def _retry_delay(res) -> float | None:
    """Google returns how long to wait. Use it rather than guessing."""
    try:
        for detail in res.json().get("error", {}).get("details", []):
            if detail.get("@type", "").endswith("RetryInfo"):
                return float(str(detail.get("retryDelay", "0s")).rstrip("s"))
    except (ValueError, TypeError, AttributeError):
        pass
    return None


def _quota_note(res) -> str:
    """The quota id and limit, so a 429 says which ceiling was hit rather than just 429."""
    try:
        for detail in res.json().get("error", {}).get("details", []):
            if detail.get("@type", "").endswith("QuotaFailure"):
                v = (detail.get("violations") or [{}])[0]
                return f"{v.get('quotaId', 'quota')} = {v.get('quotaValue', '?')}"
    except (ValueError, TypeError, AttributeError):
        pass
    return res.text[:160]


class GeminiDiagnoser:
    """Google AI Studio (Gemini). Chosen because its free tier needs no card.

    The provider is behind the `Diagnoser` protocol so that swapping it is a one-line change
    and so that nothing downstream - policy, ledger, guards, metrics - knows or cares which
    model produced a diagnosis.
    """

    #: Flash is the right tier here: this is short-context classification, run once, and the
    #: hard part is judgement about ambiguity rather than reasoning depth.
    #: Chosen empirically over gemini-2.5-flash, which has a 20-request *daily* free cap -
    #: unusable for a 1,200-case pass. This tier matched it on the hard confusion pair
    #: (10/10 ambiguous_debited caught) with enough headroom to finish a batch.
    DEFAULT_MODEL = "gemini-3.5-flash-lite"
    ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 60.0,
        max_attempts: int = 5,
        #: A wait longer than this means a daily cap rather than a burst limit. Stop and
        #: say so; the cache makes resuming after the reset free.
        max_wait: float = 120.0,
        #: Overrides `_SYSTEM_PROMPT`. Exists so a prompt candidate can be scored against
        #: batch A without editing the module - the committed default is what ships, and a
        #: candidate that does not beat it never reaches this file.
        system_prompt: str | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        if not self.api_key:
            raise RuntimeError(
                "no Gemini API key. Set GEMINI_API_KEY, or pass --provider stub to run "
                "without a model. A free key with no card is at https://aistudio.google.com/apikey"
            )
        self.name = "gemini"
        self.model = model or self.DEFAULT_MODEL
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.max_wait = max_wait
        self.system_prompt = system_prompt or _SYSTEM_PROMPT
        self._client = None

    def _http(self):
        if self._client is None:
            import httpx  # imported lazily so the stub path needs no HTTP stack at all

            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def diagnose(self, case: Case) -> Diagnosis:
        import httpx

        payload = {
            "systemInstruction": {"parts": [{"text": self.system_prompt}]},
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": json.dumps(observable(case), separators=(",", ":"))}],
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": _RESPONSE_SCHEMA,
                "temperature": 0.0,  # classification, not generation - determinism is wanted
                "maxOutputTokens": 4096,
            },
        }

        last: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                res = self._http().post(
                    self.ENDPOINT.format(model=self.model),
                    headers={"x-goog-api-key": self.api_key},
                    json=payload,
                )
                if res.status_code == 429 or res.status_code >= 500:
                    # Free tiers rate-limit by design, and Google says how long to wait.
                    # Honouring `retryDelay` beats guessing: a fixed backoff either gives
                    # up while quota is about to return, or sleeps far longer than needed.
                    delay = _retry_delay(res) or min(60.0, 2.0 * (2**attempt))
                    last = QuotaExhausted(
                        f"HTTP {res.status_code} on {self.model}: {_quota_note(res)}"
                    )
                    if delay > self.max_wait:
                        raise last  # a daily cap, not a burst limit - stop and report
                    time.sleep(delay)
                    continue
                res.raise_for_status()
                return self._parse(case, res.json())
            except httpx.HTTPError as exc:
                last = exc
                time.sleep(min(30.0, 2.0 * (2**attempt)))

        raise RuntimeError(f"{case.id}: giving up after {self.max_attempts} attempts") from last

    def _parse(self, case: Case, body: dict) -> Diagnosis:
        try:
            text = body["candidates"][0]["content"]["parts"][0]["text"]
            raw = json.loads(text)
            cause = RootCause(raw["root_cause"])
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            # A malformed answer is recorded as a real prediction with zero confidence rather
            # than dropped. Silently skipping unparseable cases would quietly remove exactly
            # the hardest ones from the accuracy figure.
            return Diagnosis(
                case.id,
                RootCause.ISSUER_TECHNICAL_DECLINE,
                0.0,
                f"unparseable model response ({type(exc).__name__})",
                self.name,
                self.model,
            )
        return Diagnosis(
            case_id=case.id,
            root_cause=cause,
            confidence=max(0.0, min(1.0, float(raw.get("confidence", 0.5)))),
            rationale=str(raw.get("rationale", ""))[:400],
            provider=self.name,
            model=self.model,
        )



class GroqDiagnoser:
    """Groq. Same prompt, same payload, same schema as the Gemini path - only the transport
    differs, so a comparison between providers is a comparison of models and nothing else.

    Chosen over Gemini for one reason that has nothing to do with quality: Gemini's free
    tier caps at 500 requests per *day* per model, which makes a 1,200-case pass a two-day
    affair and makes iterating on the prompt effectively impossible. Groq's free tier is
    generous enough that `tune on A, report on B` is a loop rather than a one-shot.
    """

    #: gpt-oss-120b over the 20b and the qwen models: on the stratified pilot it was the
    #: only one that matched Gemini on the ambiguous_debited/issuer_technical_decline pair,
    #: which is the distinction the whole taxonomy exists to make.
    DEFAULT_MODEL = "openai/gpt-oss-120b"
    ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 60.0,
        max_attempts: int = 5,
        max_wait: float = 120.0,
        #: See the note on `GeminiDiagnoser.system_prompt`.
        system_prompt: str | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("GROQ_API_KEY", "")
        if not self.api_key:
            raise RuntimeError(
                "no Groq API key. Set GROQ_API_KEY, or pass --provider stub to run without "
                "a model. A free key with no card is at https://console.groq.com/keys"
            )
        self.name = "groq"
        self.model = model or self.DEFAULT_MODEL
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.max_wait = max_wait
        self.system_prompt = system_prompt or _SYSTEM_PROMPT
        self._client = None

    def _http(self):
        if self._client is None:
            import httpx

            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def diagnose(self, case: Case) -> Diagnosis:
        import httpx

        payload = {
            "model": self.model,
            "temperature": 0.0,  # classification, not generation - determinism is wanted
            "max_completion_tokens": 4096,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(observable(case), separators=(",", ":")),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "diagnosis",
                    "schema": {**_RESPONSE_SCHEMA, "additionalProperties": False},
                },
            },
        }

        last: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                res = self._http().post(
                    self.ENDPOINT,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
                if res.status_code == 429 or res.status_code >= 500:
                    delay = float(res.headers.get("retry-after", 0)) or min(
                        60.0, 2.0 * (2**attempt)
                    )
                    last = QuotaExhausted(f"HTTP {res.status_code} on {self.model}")
                    if delay > self.max_wait:
                        raise last
                    time.sleep(delay)
                    continue
                res.raise_for_status()
                return self._parse(case, res.json())
            except httpx.HTTPError as exc:
                last = exc
                time.sleep(min(30.0, 2.0 * (2**attempt)))

        raise RuntimeError(f"{case.id}: giving up after {self.max_attempts} attempts") from last

    def _parse(self, case: Case, body: dict) -> Diagnosis:
        try:
            raw = json.loads(body["choices"][0]["message"]["content"])
            cause = RootCause(raw["root_cause"])
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            # Recorded as a real prediction with zero confidence rather than dropped -
            # silently skipping unparseable cases removes exactly the hardest ones.
            return Diagnosis(
                case.id,
                RootCause.ISSUER_TECHNICAL_DECLINE,
                0.0,
                f"unparseable model response ({type(exc).__name__})",
                self.name,
                self.model,
            )
        return Diagnosis(
            case_id=case.id,
            root_cause=cause,
            confidence=max(0.0, min(1.0, float(raw.get("confidence", 0.5)))),
            rationale=str(raw.get("rationale", ""))[:400],
            provider=self.name,
            model=self.model,
        )


# ---------------------------------------------------------------------------
# The cache — the committed artifact
# ---------------------------------------------------------------------------


#: The model arm's committed artifact. One file per provider, so switching providers can
#: never silently overwrite results produced by a different model.
_CACHE_NAMES = {
    "stub": "diagnoses.stub.jsonl",
    "gemini": "diagnoses.gemini.jsonl",
    "groq": "diagnoses.jsonl",
}


def cache_path(batch_dir: Path, provider: str) -> Path:
    return batch_dir / _CACHE_NAMES.get(provider, f"diagnoses.{provider}.jsonl")


def load_diagnoses(path: Path) -> dict[str, Diagnosis]:
    """Read a diagnosis cache. Returns {} if it does not exist yet."""
    if not path.exists():
        return {}
    out: dict[str, Diagnosis] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line := line.strip():
                d = Diagnosis.from_dict(json.loads(line))
                out[d.case_id] = d
    return out


def run(
    cases: Iterable[Case],
    diagnoser: Diagnoser,
    path: Path,
    resume: bool = True,
    progress_every: int = 25,
    rpm: int | None = None,
) -> dict[str, Diagnosis]:
    """Diagnose `cases`, appending to `path` as it goes.

    Appends per case rather than writing at the end, so a rate limit or a dropped connection
    on case 701 costs one case, not seven hundred.
    """
    done = load_diagnoses(path) if resume else {}
    todo = [c for c in cases if c.id not in done]
    if not todo:
        return done

    # Pace proactively rather than discovering the limit by being refused. Bouncing off a
    # 429 and waiting out the retry delay works, but it wastes a request and a minute each
    # time; spacing calls to the known rate is strictly cheaper.
    interval = 60.0 / rpm if rpm else 0.0

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a" if resume else "w", encoding="utf-8") as fh:
        for i, case in enumerate(todo, 1):
            started = time.monotonic()
            d = diagnoser.diagnose(case)
            fh.write(d.to_json() + "\n")
            fh.flush()  # durability beats throughput when a run takes an hour
            done[case.id] = d
            if progress_every and i % progress_every == 0:
                print(f"  {i}/{len(todo)}  {case.id} -> {d.root_cause} ({d.confidence:.2f})",
                      flush=True)
            if interval:
                time.sleep(max(0.0, interval - (time.monotonic() - started)))
    return done


def build(
    provider: str, model: str | None = None, system_prompt: str | None = None
) -> Diagnoser:
    if provider == "stub":
        return StubDiagnoser()
    if provider == "gemini":
        return GeminiDiagnoser(model=model, system_prompt=system_prompt)
    if provider == "groq":
        return GroqDiagnoser(model=model, system_prompt=system_prompt)
    raise ValueError(f"unknown provider {provider!r}; known: stub, gemini, groq")


def main() -> int:
    import argparse

    from reclaim.core import feed

    ap = argparse.ArgumentParser(description="Diagnose a batch's root causes, once.")
    ap.add_argument("--batch", default="B")
    ap.add_argument("--root", default="data")
    ap.add_argument("--provider", default="stub", choices=["stub", "gemini", "groq"])
    ap.add_argument("--model", default=None)
    ap.add_argument("--limit", type=int, default=None, help="diagnose only the first N cases")
    ap.add_argument("--rpm", type=int, default=None, help="pace calls to this rate")
    ap.add_argument("--no-resume", action="store_true", help="rewrite the cache from scratch")
    args = ap.parse_args()

    b = feed.load_batch(args.batch, args.root)
    cases = b.cases[: args.limit] if args.limit else b.cases
    diagnoser = build(args.provider, args.model)
    path = cache_path(b.dir, args.provider)

    print(f"batch {b.name}  {len(cases)} cases  provider={diagnoser.name} model={diagnoser.model}")
    print(f"cache: {path}")
    started = time.time()
    out = run(cases, diagnoser, path, resume=not args.no_resume, rpm=args.rpm)
    print(f"{len(out)} diagnoses in {time.time() - started:.1f}s")

    counts: dict[str, int] = {}
    for d in out.values():
        counts[str(d.root_cause)] = counts.get(str(d.root_cause), 0) + 1
    for cause, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {n:5d}  {cause}")
    print(f"\nscore it: python -m reclaim.eval.confusion --batch {b.name} --provider {args.provider}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
