from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Optional

from .deposit_models import (
    DEPOSIT_STATUS_LABELS,
    DepositCheckResult,
    DepositRecord,
    RefundLine,
    STATUS_CLEARED,
    STATUS_OUTSTANDING,
    STATUS_PENDING,
    TX_ARRIVAL,
    TX_ERRATUM,
    TX_LABELS,
    TX_REFUND,
)
from .parsing import format_datetime, unique_preserve_order


def _amount_text(value: Optional[Decimal]) -> str:
    return "未标注" if value is None else f"{value:.2f}"


def _group_by_serial(
    records: list[DepositRecord], tx_type: str
) -> dict[str, list[DepositRecord]]:
    groups: dict[str, list[DepositRecord]] = defaultdict(list)
    for record in records:
        serial = (record.serial or "").strip()
        if record.tx_type == tx_type and serial:
            groups[serial].append(record)
    return groups


def _serial_amount_conflicts(
    groups: dict[str, list[DepositRecord]], label: str
) -> tuple[list[str], set[str]]:
    """Report same-serial amount conflicts and return the affected serials."""

    issues: list[str] = []
    conflicted: set[str] = set()
    for serial, group in groups.items():
        amounts = {item.amount for item in group if item.fetch_ok}
        if len(amounts) > 1:
            conflicted.add(serial)
            values = "、".join(
                _amount_text(amount)
                for amount in sorted(amounts, key=lambda value: (value is None, value or 0))
            )
            issues.append(
                f"{label}流水号 {serial} 出现金额冲突（{values}），无法确定唯一金额"
            )
    return issues, conflicted


def _same_moment_errata_conflicts(errata: list[DepositRecord]) -> list[str]:
    """Find errata published at the same moment that disagree on an amount.

    Only errata for the *same* referenced refund can conflict with each other;
    two errata published at the same minute for different refunds are unrelated.
    """

    issues: list[str] = []
    groups: dict[str, list[DepositRecord]] = defaultdict(list)
    for record in errata:
        if record.fetch_ok and record.reference and record.publish_time is not None:
            groups[record.reference.strip()].append(record)

    for reference, group in groups.items():
        by_moment: dict[datetime, list[DepositRecord]] = defaultdict(list)
        for record in group:
            # Publication times are usually minute precision; compare by minute.
            minute = record.publish_time.replace(second=0, microsecond=0)
            by_moment[minute].append(record)
        for minute, same_time in by_moment.items():
            if len(same_time) < 2:
                continue
            amounts = {item.amount for item in same_time}
            if len(amounts) > 1:
                values = "、".join(
                    _amount_text(amount)
                    for amount in sorted(amounts, key=lambda value: (value is None, value or 0))
                )
                issues.append(
                    f"{format_datetime(minute)} 针对退款流水号 {reference} 的勘误互相冲突"
                    f"（金额 {values}），无法确定以哪条为准"
                )
    return issues


def _serial_type_conflicts(records: list[DepositRecord]) -> list[str]:
    """Flag a serial reused for more than one kind of ledger entry."""

    types_by_serial: dict[str, set[str]] = defaultdict(set)
    for record in records:
        serial = (record.serial or "").strip()
        if serial and record.fetch_ok:
            types_by_serial[serial].add(record.tx_type)
    issues: list[str] = []
    for serial, types in types_by_serial.items():
        if len(types) > 1:
            labels = "、".join(
                TX_LABELS.get(tx_type, tx_type)
                for tx_type in sorted(types)
            )
            issues.append(
                f"流水号 {serial} 同时出现在不同明细类型（{labels}），无法按流水号唯一识别"
            )
    return issues


def _dedupe_records(records: list[DepositRecord]) -> tuple[list[DepositRecord], int]:
    """Keep one row per type+serial, preferring a successfully fetched copy.

    Amount conflicts are checked on the raw rows before this step, so a
    discarded duplicate cannot hide a discrepancy.
    """

    result: list[DepositRecord] = []
    index_by_key: dict[str, int] = {}
    duplicates = 0

    for record in records:
        serial = (record.serial or "").strip()
        key = f"{record.tx_type}:{serial}" if serial else f"url:{record.url}"
        if key in index_by_key:
            duplicates += 1
            position = index_by_key[key]
            existing = result[position]
            # Prefer a fetched copy. If both were fetched, keep the first.
            if not existing.fetch_ok and record.fetch_ok:
                result[position] = record
            continue
        index_by_key[key] = len(result)
        result.append(record)

    return result, duplicates


def resolve_deposit_check(
    *,
    project_no: str,
    receipt_no: str,
    directory_url: str,
    records: list[DepositRecord],
    issues: list[str],
    failed_details: list[DepositRecord],
    pages: list[str],
    duplicates_skipped: int,
    detail_count: int,
    now: datetime,
) -> DepositCheckResult:
    """Reconcile a receipt's arrival, refunds and errata.

    The result deliberately withholds ``outstanding_amount`` whenever the
    evidence is incomplete or contradictory.
    """

    result_issues = list(issues)

    # Conflicts are checked on the raw rows, before serial-based de-duplication.
    raw_errata = [item for item in records if item.tx_type == TX_ERRATUM]

    arrival_conflicts, conflicted_arrivals = _serial_amount_conflicts(
        _group_by_serial(records, TX_ARRIVAL), "到账"
    )
    refund_conflicts, conflicted_refunds = _serial_amount_conflicts(
        _group_by_serial(records, TX_REFUND), "退款"
    )
    errata_conflicts = _same_moment_errata_conflicts(raw_errata)
    result_issues.extend(arrival_conflicts)
    result_issues.extend(refund_conflicts)
    result_issues.extend(errata_conflicts)
    result_issues.extend(_serial_type_conflicts(records))

    deduped, extra_duplicates = _dedupe_records(records)
    duplicates = duplicates_skipped + extra_duplicates

    arrivals = [item for item in deduped if item.tx_type == TX_ARRIVAL]
    refunds_raw = [item for item in deduped if item.tx_type == TX_REFUND]
    errata = [item for item in deduped if item.tx_type == TX_ERRATUM]

    failed = [item for item in deduped if not item.fetch_ok]
    for record in failed:
        result_issues.append(
            f"明细抓取失败：《{record.title}》（{record.url}）{record.error or ''}"
        )
    if failed and not failed_details:
        failed_details = failed

    # ---------------------------------------------------------------- arrivals
    arrival_amount: Optional[Decimal] = None
    valid_arrivals = [item for item in arrivals if item.fetch_ok]
    if not valid_arrivals:
        result_issues.append("未找到该收据的有效到账记录，无法确定应退总额")
    else:
        amounts = {item.amount for item in valid_arrivals}
        if None in amounts:
            result_issues.append("到账记录未标注金额，无法确定应退总额")
        elif len(amounts) > 1 and not arrival_conflicts:
            # Multiple arrival serials with different amounts could be partial
            # payments, but the evidence does not say how to treat them. Do not
            # invent an ordering or a sum.
            values = "、".join(f"{amount:.2f}" for amount in sorted(amounts))
            result_issues.append(
                f"存在多条金额不一致的到账记录（{values}），无法确定应退总额"
            )
        elif not conflicted_arrivals and len(amounts) == 1:
            arrival_amount = next(iter(amounts))
            if len(valid_arrivals) > 1:
                result_issues.append(
                    "该收据存在多条金额相同的到账记录，无法确认是重复登记还是分笔到账"
                )

    # ----------------------------------------------------------------- errata
    serial_map: dict[str, DepositRecord] = {}
    for record in deduped:
        if record.serial:
            serial_map.setdefault(record.serial.strip(), record)

    errata_by_reference: dict[str, list[DepositRecord]] = defaultdict(list)
    usable_errata: list[DepositRecord] = []
    for record in errata:
        if not record.fetch_ok:
            continue
        if not record.reference:
            result_issues.append(
                f"勘误《{record.title}》未标注引用的退款流水号，无法确定更正对象"
            )
            continue
        reference = record.reference.strip()
        target = serial_map.get(reference)
        if target is None:
            result_issues.append(
                f"勘误《{record.title}》引用的流水号 {reference} 不存在，无法确定更正对象"
            )
            continue
        if target.tx_type != TX_REFUND:
            result_issues.append(
                f"勘误《{record.title}》引用的流水号 {reference} 不是退款记录"
                f"（类型为{target.type_label}），无法替换退款金额"
            )
            continue
        if record.amount is None:
            result_issues.append(
                f"勘误《{record.title}》未标注更正金额，无法确定退款金额"
            )
            continue
        if record.publish_time is None:
            result_issues.append(
                f"勘误《{record.title}》缺少发布时间，无法按发布时间确定先后"
            )
            continue
        errata_by_reference[reference].append(record)
        usable_errata.append(record)

    # ----------------------------------------------------------------- refunds
    refunds: list[RefundLine] = []
    for record in refunds_raw:
        serial = (record.serial or "").strip()
        refunds.append(
            RefundLine(
                serial=serial,
                publish_time=record.publish_time,
                amount=record.amount,
                original_amount=record.amount,
                url=record.url,
                title=record.title,
                found_on=record.found_on,
                errata=list(errata_by_reference.get(serial, [])),
                fetch_ok=record.fetch_ok,
            )
        )

    for line, record in zip(refunds, refunds_raw):
        if not line.serial:
            if record.fetch_ok:
                result_issues.append(
                    f"退款《{line.title}》缺少流水号，无法按流水号去重并核对勘误引用"
                )

        candidates = [
            item
            for item in line.errata
            if item.amount is not None and item.publish_time is not None
        ]
        if candidates:
            # Do not pick one of several errata published at the same moment
            # with different amounts; the conflict was already reported.
            by_moment: dict[datetime, list[DepositRecord]] = defaultdict(list)
            for item in candidates:
                by_moment[item.publish_time.replace(second=0, microsecond=0)].append(item)
            conflicted_moments = [
                minute
                for minute, group in by_moment.items()
                if len({item.amount for item in group}) > 1
            ]
            if conflicted_moments:
                line.conflict_note = "同一发布时间存在金额互相冲突的勘误，未采用其中任何一条"
            else:
                latest_time = max(by_moment)
                chosen = by_moment[latest_time][0]
                line.amount = chosen.amount
                line.corrected = True
                chosen.applied_to = line.serial

        if line.amount is None and line.serial and line.serial not in conflicted_refunds:
            result_issues.append(
                f"退款流水号 {line.serial} 未标注可用金额，无法汇总已退金额"
            )
        if line.serial in conflicted_refunds and not line.corrected:
            line.conflict_note = "退款流水号存在金额冲突，未确定唯一金额"

    refund_total = Decimal("0")
    refund_total_known = True
    for line in refunds:
        if line.amount is None:
            refund_total_known = False
            continue
        refund_total += line.amount

    # ---------------------------------------------------------------- totals
    outstanding: Optional[Decimal] = None
    if arrival_amount is not None and refund_total_known:
        if refund_total > arrival_amount:
            result_issues.append(
                f"退款合计 {refund_total:.2f} 元超过到账金额 {arrival_amount:.2f} 元，"
                "差额异常，需人工核实"
            )
        else:
            outstanding = (arrival_amount - refund_total).quantize(Decimal("0.01"))

    fatal = bool(result_issues or failed)
    if fatal:
        status = STATUS_PENDING
        headline = "存在未决或冲突信息，暂不能给出确定未退金额"
        display_outstanding = None
    elif arrival_amount is None or not refund_total_known or outstanding is None:
        status = STATUS_PENDING
        headline = "关键金额缺失，暂不能计算未退金额"
        display_outstanding = None
    elif outstanding == 0:
        status = STATUS_CLEARED
        headline = "该收据保证金已全部退还，未退金额为 0"
        display_outstanding = Decimal("0.00")
    else:
        status = STATUS_OUTSTANDING
        headline = "该收据仍有保证金未退还"
        display_outstanding = outstanding

    notes: list[str] = []
    if pages:
        notes.append(f"已核对 {len(pages)} 个目录页")
    notes.append(f"按流水号去重后纳入 {len(deduped)} 条明细")
    if duplicates:
        notes.append(f"跨页或重复明细已合并 {duplicates} 条")
    if usable_errata:
        applied = [item for item in usable_errata if item.applied_to]
        notes.append(
            f"读取勘误 {len(usable_errata)} 条，已应用 {len(applied)} 条，按发布时间取最新金额"
        )
    elif errata:
        notes.append(f"读取勘误 {len(errata)} 条，均未能安全应用")
    if refunds:
        notes.append(f"共 {len(refunds)} 笔退款")
    if arrivals:
        notes.append(f"到账记录 {len(arrivals)} 条")

    return DepositCheckResult(
        project_no=project_no,
        receipt_no=receipt_no,
        directory_url=directory_url,
        status=status,
        status_label=DEPOSIT_STATUS_LABELS[status],
        headline=headline,
        checked_at=now,
        arrival_amount=arrival_amount,
        refunded_amount=refund_total if refund_total_known else None,
        outstanding_amount=display_outstanding,
        arrival_records=arrivals,
        refunds=refunds,
        errata=errata,
        records=deduped,
        failed_details=failed,
        issues=unique_preserve_order(result_issues),
        notes=notes,
        pages=pages,
        duplicates_skipped=duplicates,
        detail_count=detail_count,
    )
