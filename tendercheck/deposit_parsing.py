"""Parsing helpers for the deposit refund reconciliation feature.

The deposit pages use a different vocabulary from the announcement pages, so
these helpers stay separate from ``tendercheck.parsing`` while reusing the
normalization and date utilities from it.
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Optional

from .parsing import clean_text, find_datetime_candidates, normalize_project

TXTYPE_ARRIVAL = "arrival"
TXTYPE_REFUND = "refund"
TXTYPE_ERRATUM = "erratum"
TX_TYPES = {TXTYPE_ARRIVAL, TXTYPE_REFUND, TXTYPE_ERRATUM}

_RECEIPT_LABEL_RE = re.compile(
    r"(?:收据|票据|凭证)(?:编号|号|编码)?\s*[：:]\s*(?P<value>[A-Za-z0-9_\-./]+)",
)
_PROJECT_LABEL_RE = re.compile(
    r"(?:项目|招标|采购|标段)(?:编号|编码|号)\s*[：:]\s*(?P<value>[A-Za-z0-9_\-./（）()]+)",
)
_TX_LABEL_RE = re.compile(
    r"(?:业务类型|流水类型|交易类型|类型|款项类型|记录类型)\s*[：:]\s*(?P<value>[^\s，。；;、]{1,16})"
)
_SERIAL_LABEL_RE = re.compile(
    r"(?<!退款)(?<!引用)(?<!原)(?<!所引)(?<!对应)(?<!针对)"
    r"(?:流水号|交易流水号|业务流水号|银行流水号|回单号)\s*[：:]\s*"
    r"(?P<value>[A-Za-z0-9_\-./]+)"
)
_AMOUNT_LABEL_RE = re.compile(
    r"(?<!原)(?:退款金额|到账金额|入账金额|金额)\s*[：:]\s*[¥￥]?\s*(?P<value>-?\d[\d,，]*(?:\.\d+)?)\s*(?:元|人民币)?"
)
_CORRECTED_AMOUNT_RE = re.compile(
    r"(?:更正后(?:退款)?金额|调整后(?:退款)?金额|更正为|调整为|修正为|应退金额|现为|应为)"
    r"\s*[：:为]?\s*[¥￥]?\s*(?P<value>-?\d[\d,，]*(?:\.\d+)?)\s*(?:元|人民币)?"
)
_ORIGINAL_AMOUNT_RE = re.compile(
    r"(?:原退款金额|原金额|更正前金额|更正前)\s*[：:]\s*[¥￥]?\s*(?P<value>\d[\d,，]*(?:\.\d+)?)\s*(?:元|人民币)?"
)
REFERENCE_LABEL_RE = re.compile(
    r"(?:勘误引用|引用流水号|针对流水号|原流水号|所引流水号|对应流水号|更正对象|原记录)\s*[：:]\s*(?P<value>[A-Za-z0-9_\-./]+)"
)
_PUBLISH_LABEL_RE = re.compile(
    r"(?:发布时间|公告时间|勘误时间|记账时间|入账时间|退款时间|到账时间|更新时间|公示时间|日期)\s*[：:]\s*"
)

_ARRIVAL_RE = re.compile(r"(?:到账|入账|收款|缴纳|缴存|保证金到账|已收)")
_REFUND_RE = re.compile(r"(?:退款|退还|退回|退付|返还|已退)")
_ERRATUM_RE = re.compile(r"(?:勘误|更正|纠正|修正|调整|变更)")


def parse_amount(value: str | None) -> Optional[Decimal]:
    if not value:
        return None
    text = unicodedata.normalize("NFKC", clean_text(value))
    text = text.replace(",", "").replace("，", "").replace("￥", "").replace("¥", "")
    text = re.sub(r"\s+", "", text)
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return Decimal(match.group(0)).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def format_amount(value: Optional[Decimal]) -> str:
    if value is None:
        return "—"
    return f"{value.quantize(Decimal('0.01')):,.2f}"


def detect_deposit_type(text: str | None, explicit_type: str | None = None) -> Optional[str]:
    """Classify a deposit ledger entry by its explicit type or title.

    Erratum wins over the underlying transaction type because a page titled
    “退款勘误” is a correction record, not another refund.
    """

    primary = clean_text(f"{explicit_type or ''} {text or ''}")
    if _ERRATUM_RE.search(primary):
        return TXTYPE_ERRATUM
    if _REFUND_RE.search(primary):
        return TXTYPE_REFUND
    if _ARRIVAL_RE.search(primary):
        return TXTYPE_ARRIVAL
    return None


def extract_receipt_no(text: str | None) -> Optional[str]:
    if not text:
        return None
    match = _RECEIPT_LABEL_RE.search(unicodedata.normalize("NFKC", clean_text(text)))
    return match.group("value").strip("，。；;、") if match else None


def extract_project_no(text: str | None) -> Optional[str]:
    if not text:
        return None
    match = _PROJECT_LABEL_RE.search(unicodedata.normalize("NFKC", clean_text(text)))
    return match.group("value").strip("，。；;、") if match else None


def extract_explicit_type(text: str | None) -> Optional[str]:
    if not text:
        return None
    match = _TX_LABEL_RE.search(unicodedata.normalize("NFKC", clean_text(text)))
    return match.group("value") if match else None


def extract_type_from_context(context: str | None) -> Optional[str]:
    """Read a type from the .notice-type / data-* metadata around a link."""

    if not context:
        return None
    for pattern in (
        r"数据(?:类型)?\s*[：:]\s*(?P<value>[^\s，。；;]{1,16})",
        r"(?:业务类型|流水类型|交易类型|记录类型)\s*[：:]\s*(?P<value>[^\s，。；;]{1,16})",
    ):
        match = re.search(pattern, context)
        if match:
            return match.group("value")
    return None


def extract_serial(text: str | None) -> Optional[str]:
    if not text:
        return None
    match = _SERIAL_LABEL_RE.search(unicodedata.normalize("NFKC", clean_text(text)))
    return match.group("value").strip("，。；;、") if match else None


def extract_amount(text: str | None) -> Optional[Decimal]:
    if not text:
        return None
    match = _AMOUNT_LABEL_RE.search(unicodedata.normalize("NFKC", clean_text(text)))
    return parse_amount(match.group("value")) if match else None


def extract_erratum_amount(text: str | None) -> Optional[Decimal]:
    """Read the corrected amount from an erratum page.

    Errata may state “更正后金额：…”, or simply “现为…元”. A plain “退款金额”
    label is accepted as a final fallback.
    """

    if not text:
        return None
    normalized = unicodedata.normalize("NFKC", clean_text(text))
    for pattern in (_CORRECTED_AMOUNT_RE, _AMOUNT_LABEL_RE):
        match = pattern.search(normalized)
        if match:
            return parse_amount(match.group("value"))
    return None


def extract_original_amount(text: str | None) -> Optional[Decimal]:
    if not text:
        return None
    match = _ORIGINAL_AMOUNT_RE.search(unicodedata.normalize("NFKC", clean_text(text)))
    return parse_amount(match.group("value")) if match else None


def extract_reference(text: str | None) -> Optional[str]:
    if not text:
        return None
    match = REFERENCE_LABEL_RE.search(unicodedata.normalize("NFKC", clean_text(text)))
    return match.group("value").strip("，。；;、") if match else None


def extract_labeled_publish_time(text: str | None):
    """Extract a publication/effective time after a recognized label."""

    if not text:
        return None
    normalized = unicodedata.normalize("NFKC", clean_text(text))
    for label in _PUBLISH_LABEL_RE.finditer(normalized):
        candidates = find_datetime_candidates(normalized[label.end() : label.end() + 60])
        if candidates:
            return candidates[0]["datetime"]
    return None


def receipt_matches(text: str | None, receipt_no: str) -> bool:
    """Return True when the receipt number appears as a whole token."""

    key = normalize_project(receipt_no)
    haystack = unicodedata.normalize("NFKC", clean_text(text)).upper()
    if not key or not haystack:
        return False
    start = haystack.find(key)
    while start != -1:
        end = start + len(key)
        before = haystack[start - 1] if start > 0 else ""
        after = haystack[end] if end < len(haystack) else ""
        if (not before or not before.isalnum()) and (not after or not after.isalnum()):
            return True
        start = haystack.find(key, start + 1)
    return False


def project_matches(text: str | None, project_no: str) -> bool:
    return receipt_matches(text, project_no)
