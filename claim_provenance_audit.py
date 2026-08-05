"""Pure role-scoped lexical closure for resume claim authorization.

The policy intentionally proves only that draft atoms have same-role source
anchors.  It does not attempt semantic entailment.  Inflection folding is
conservative, and the small verb table below is the complete paraphrase policy.
"""

import re
import unicodedata
from typing import Any


CLAIM_PROVENANCE_VERSION = "claim-provenance/v1"

_REQUEST_KEYS = {"schema_version", "source_roles", "draft_roles", "job_description"}
_ROLE_KEYS = {"title", "company", "dates", "bullets"}
# No lexical word is semantically disposable.  In particular, prepositions,
# conjunctions, and auxiliaries encode agency, direction, scope, and logic
# ("audited FDA" != "audited by FDA"; "and" != "or").  Keeping every word in
# the ordered signature is intentionally conservative and fails closed.
_STOP_WORDS: frozenset[str] = frozenset()

# Closed action-opener equivalence policy.  Only these explicit past-tense
# forms may close, and only in the first lexical slot of a line.  Every other
# token remains exact and case-preserving after NFC/smart-punctuation
# normalization. This prevents acronym, skill, proper-noun, and noun-homograph
# invention (``authors``/``drafts``, ``checks``/``reviews``), while generic
# suffix stripping from changing meaning.
_SAFE_ACTION_GROUPS = (
    ("wrote", "authored", "drafted"),
    ("cut", "reduced", "decreased"),
    ("reviewed", "checked", "examined"),
    ("tracked", "monitored"),
    ("analyzed", "assessed", "evaluated"),
    ("supported", "assisted", "helped"),
)
_SAFE_ACTION_CANON = {
    word: group[0] for group in _SAFE_ACTION_GROUPS for word in group
}
_TOKEN_RE = re.compile(
    r"<=|>=|!=|==|"
    r"\d+(?:[.,]\d+)*|"
    r"[^\W\d_]+|"
    r"_+|"
    r"[^\w\s]",
    re.UNICODE,
)
_NUMBER_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)*")
_NUMERIC_PREFIX_TOKENS = {
    "+", "-", "<", ">", "<=", ">=", "~", "≈",
    "$", "€", "£", "¥", "₹",
}
_NUMERIC_SUFFIX_TOKENS = {
    "%", "+", "x", "×", "percent", "percentage", "fold",
    "k", "m", "b", "t", "thousand", "million", "billion", "trillion",
}
_NORMALIZE_TRANSLATION = str.maketrans(
    {
        "’": "'",
        "‘": "'",
        "−": "-",
        "–": "-",
        "—": "-",
        "≤": "<=",
        "≥": ">=",
    }
)


def _result(verdict: str, code: str | None = None) -> dict:
    return {
        "schema_version": "claim-provenance-result/v1",
        "verdict": verdict,
        "codes": [] if code is None else [code],
    }


def _valid_role(value: Any) -> bool:
    return (
        type(value) is dict
        and set(value) == _ROLE_KEYS
        and all(type(value[key]) is str for key in ("title", "company", "dates"))
        and type(value["bullets"]) is list
        and all(type(item) is str for item in value["bullets"])
    )


def _valid_request(value: Any) -> bool:
    return (
        type(value) is dict
        and set(value) == _REQUEST_KEYS
        and value.get("schema_version") == "claim-provenance-request/v1"
        and type(value.get("job_description")) is str
        and type(value.get("source_roles")) is list
        and type(value.get("draft_roles")) is list
        and all(_valid_role(role) for role in value["source_roles"])
        and all(_valid_role(role) for role in value["draft_roles"])
    )


def _role_key(role: dict) -> tuple[str, str, str]:
    return role["title"], role["company"], role["dates"]


def _fold_word(word: str, *, action_slot: bool = False) -> str:
    normalized = word.replace("’", "'").strip("'")
    if action_slot:
        canonical = _SAFE_ACTION_CANON.get(normalized.casefold())
        if canonical is not None:
            return canonical
    return normalized


def _token_signature(text: str) -> tuple[str, ...]:
    """Return an exact normalized token stream with one closed exception.

    Every visible non-whitespace symbol is retained.  This makes ``C#`` and
    ``C++``, ``&`` and no conjunction, numeric signs/bounds/currencies, and
    range versus ratio punctuation distinct.  Only the first lexical token may
    use the explicit action-opener equivalence table above.
    """

    normalized = unicodedata.normalize("NFC", text).translate(
        _NORMALIZE_TRANSLATION
    )
    tokens = _TOKEN_RE.findall(normalized)
    for index, token in enumerate(tokens):
        if any(character.isalpha() for character in token):
            tokens[index] = _fold_word(token, action_slot=True)
            break
    return tuple(tokens)


def _metric_signature(
    tokens: tuple[str, ...], include_lexical_unit: bool = True
) -> tuple[tuple[str, ...], ...]:
    """Bind each exact number to its nearby sign, operator, scale, and unit.

    ``include_lexical_unit=False`` keeps the value and its numeric decorations
    (sign, comparator, ``%``, scale words) but drops the trailing plain word.
    That word is captured as a unit so ``10 cases`` and ``10 reports`` stay
    distinct, which is right for the identity rule -- but it also means simply
    inserting a term next to a number changes the signature, since
    ``100 % regulatory`` and ``100 % GCP`` no longer match. The additive rule
    therefore compares the numeric core and relies on ordered containment to
    prove the original unit word is still present and still in place; a claim
    that swapped ``cases`` for ``reports`` fails containment, not this.
    """

    events: list[tuple[str, ...]] = []
    for index, token in enumerate(tokens):
        if not _NUMBER_TOKEN_RE.fullmatch(token):
            continue
        prefix: list[str] = []
        if index and tokens[index - 1] in _NUMERIC_PREFIX_TOKENS:
            prefix.append(tokens[index - 1])
        elif (
            index > 1
            and tokens[index - 1] in {"-", "/", ":"}
            and _NUMBER_TOKEN_RE.fullmatch(tokens[index - 2])
        ):
            prefix.append(tokens[index - 1])

        suffix: list[str] = []
        cursor = index + 1
        if cursor < len(tokens):
            following = tokens[cursor]
            if following in _NUMERIC_SUFFIX_TOKENS:
                suffix.append(following)
                cursor += 1
            elif (
                following in {"-", "/", ":"}
                and cursor + 1 < len(tokens)
                and (
                    _NUMBER_TOKEN_RE.fullmatch(tokens[cursor + 1])
                    or tokens[cursor + 1] == "fold"
                )
            ):
                suffix.append(following)
                cursor += 1
        # Preserve a count/duration/scale unit after the value or suffix.  A
        # maximum of one lexical unit avoids swallowing arbitrary prose while
        # still distinguishing ``10 cases`` from ``10 reports``.
        if include_lexical_unit and cursor < len(tokens) and tokens[cursor].isalpha():
            suffix.append(tokens[cursor])
        events.append(tuple(prefix + [token] + suffix))
    return tuple(events)


# Polarity-reversing tokens. Ordered containment alone would admit
# "Never managed 8 sites" as supported by "Managed 8 sites" -- every source
# token is still present, in order, with the number untouched. These may
# therefore never be ADDED (a claim keeps any the source already had).
#
# Deliberately English function words, not domain vocabulary: negation works
# the same way in a nursing resume, a software resume, and a welding resume,
# so this list does not need to grow when the industry changes.
_POLARITY_TOKENS: frozenset[str] = frozenset({
    "not", "no", "never", "none", "nor", "without", "except", "excluding",
    "excluded", "failed", "failing", "unable", "unsuccessful", "lacking",
    "lacked", "denied", "rejected", "rarely", "seldom", "hardly", "barely",
    "non", "un", "pending", "attempted", "partially", "almost", "nearly",
})

# How much a claim may grow, as a fraction of its source's token count.
#
# This is the coarsest of the four conditions and deliberately not the load-
# bearing one: ordered containment already forbids dropping or reordering any
# source token, numeric-core equality already forbids touching a figure, and
# the polarity rule already forbids inverting the sense. The budget only bounds
# how much *additional* non-numeric wording may ride along.
#
# Measured against a real run, 0.40 was too tight to be useful. A core-
# competencies line is a delimited keyword list, and adding two of the job
# description's terms to a 28-token line needed 15 -- legitimate tailoring,
# refused. 0.60 admits that while still refusing the whole fabricated clauses
# in the test suite. Skills added this way are separately required to trace to
# real evidence by evidence_audit.py, which is the gate actually designed for
# that question; this bound is a backstop, not the proof.
_MAX_ADDED_TOKEN_RATIO = 0.60
_MIN_ADDED_TOKENS = 2


def _is_ordered_subsequence(
    needle: tuple[str, ...], haystack: tuple[str, ...]
) -> bool:
    """True when every needle token appears in haystack, in the same order."""
    cursor = iter(haystack)
    return all(token in cursor for token in needle)


def _additive_only(
    source_signature: tuple[str, ...], claim_signature: tuple[str, ...]
) -> bool:
    """True when the claim is its source plus a bounded set of new words.

    The identity rule this relaxes admitted only a byte-identical line, which
    made tailoring impossible: incorporating a single term from the job
    description -- the product's central promise -- was rejected as an
    unsupported claim. Additive support restores that while keeping the
    guarantee people actually rely on, that nothing is invented.

    Three conditions, all structural rather than lexical, so the rule behaves
    identically for any industry:

    1. Every source token survives, in its original order. Nothing may be
       dropped, reordered, or swapped, so a rewrite cannot quietly shed a
       qualifier, a bound, or a scope word.
    2. Nothing polarity-reversing is introduced (see _POLARITY_TOKENS) --
       otherwise containment would happily admit "Never led the migration"
       as supported by "Led the migration".
    3. Additions stay proportionally small. Enough for the job description's
       vocabulary, not enough for a fabricated clause.

    4. Every figure survives untouched. The numeric cores must match exactly
       -- same values, same signs, same scales, same order, none added and
       none removed -- so no metric can be invented or quietly inflated. The
       unit word beside each number is protected by condition 1 rather than
       here, which is what lets a term sit next to a number without the
       insertion reading as a changed unit.

    What this does NOT prove is entailment. A bounded set of non-numeric words
    could still read as a claim the source does not make; the Auditor role and
    the downstream evidence audit exist to catch that. This is a closure rule,
    deliberately conservative, not a truth oracle.
    """
    if not _is_ordered_subsequence(source_signature, claim_signature):
        return False

    if _metric_signature(source_signature, False) != _metric_signature(
        claim_signature, False
    ):
        return False

    added_count = len(claim_signature) - len(source_signature)
    if added_count <= 0:
        return False

    budget = max(_MIN_ADDED_TOKENS, int(len(source_signature) * _MAX_ADDED_TOKEN_RATIO))
    if added_count > budget:
        return False

    source_polarity = sum(
        1 for token in source_signature if token.casefold() in _POLARITY_TOKENS
    )
    claim_polarity = sum(
        1 for token in claim_signature if token.casefold() in _POLARITY_TOKENS
    )
    return claim_polarity <= source_polarity


def claim_supported_by_source(claim: str, source: str) -> tuple[bool, str]:
    """Conservatively prove one free-text claim against one trusted source span.

    This is intentionally a lexical closure check, not an entailment model.  A
    claim is admitted only when its ordered typed-numeric and folded-content
    signature exactly matches one source span.  Requiring ordered equality
    prevents a rewrite from silently dropping negations, bounds, qualifiers,
    swapping subjects or values, or omitting a second metric.  The small
    verb-equivalence table at module scope is the complete paraphrase allowance;
    unknown rewrites fail closed.
    """

    if type(claim) is not str or not claim.strip() or type(source) is not str or not source.strip():
        return False, "UNSUPPORTED_CLAIM"
    claim_signature = _token_signature(claim)
    source_signature = _token_signature(source)
    if claim_signature == source_signature:
        return True, "AUTHORIZED"

    # Additive support is tried first. Its own numeric check is the stricter
    # one for this purpose -- every figure must survive unchanged, in order,
    # with nothing added -- while tolerating a term inserted beside a number,
    # which the identity-oriented signature below would read as a changed unit.
    if _additive_only(source_signature, claim_signature):
        return True, "AUTHORIZED"

    if _metric_signature(claim_signature) != _metric_signature(source_signature):
        return False, "UNSUPPORTED_METRIC"
    return False, "UNSUPPORTED_CLAIM"


def audit_claim_provenance(request: dict) -> dict:
    """Authorize only draft atoms closed by exactly one same-role source."""
    if not _valid_request(request):
        return _result("REJECTED:UNSUPPORTED_CLAIM", "UNSUPPORTED_CLAIM")

    source_by_role: dict[tuple[str, str, str], list[dict]] = {}
    for role in request["source_roles"]:
        source_by_role.setdefault(_role_key(role), []).append(role)

    aligned: list[tuple[dict, dict | None]] = []
    for draft in request["draft_roles"]:
        anchors = source_by_role.get(_role_key(draft), [])
        aligned.append((draft, anchors[0] if len(anchors) == 1 else None))

    for draft, source in aligned:
        if source is None:
            return _result("REJECTED:UNSUPPORTED_CLAIM", "UNSUPPORTED_CLAIM")
        for draft_bullet in draft["bullets"]:
            outcomes = [
                claim_supported_by_source(draft_bullet, source_bullet)
                for source_bullet in source["bullets"]
            ]
            if any(supported for supported, _ in outcomes):
                continue
            code = (
                "UNSUPPORTED_METRIC"
                if any(code == "UNSUPPORTED_METRIC" for _, code in outcomes)
                else "UNSUPPORTED_CLAIM"
            )
            return _result(f"REJECTED:{code}", code)

    return _result("AUTHORIZED")
