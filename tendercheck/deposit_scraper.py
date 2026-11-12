from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .deposit_models import DepositCheckResult, DepositRecord, TX_ARRIVAL, TX_REFUND, TX_ERRATUM
from .deposit_parsing import (
    TX_TYPES,
    detect_deposit_type,
    extract_amount,
    extract_erratum_amount,
    extract_explicit_type,
    extract_labeled_publish_time,
    extract_original_amount,
    extract_project_no,
    extract_receipt_no,
    extract_reference,
    extract_serial,
    extract_type_from_context,
    receipt_matches,
)
from .deposit_resolution import resolve_deposit_check
from .parsing import BEIJING_TZ, clean_text, normalize_project, parse_datetime
from .scraper import (
    FetchError,
    HttpFetcher,
    InputError,
    ListFetchError,
    anchor_text,
    canonical_url,
    is_pagination_link,
    same_site,
    url_origin,
    validate_url,
)


@dataclass
class DepositCandidate:
    url: str
    title: str
    context: str
    found_on: str
    list_time: Optional[datetime]
    related: bool


class DepositScraper:
    """Read a receipt's deposit ledger from a same-site paginated directory."""

    def __init__(
        self,
        *,
        project_no: str,
        receipt_no: str,
        directory_url: str,
        fetcher: Optional[HttpFetcher] = None,
        max_pages: int = 15,
        max_details: int = 80,
    ) -> None:
        project_no = clean_text(project_no)
        receipt_no = clean_text(receipt_no)
        if not project_no:
            raise InputError("请输入项目编号")
        if not receipt_no:
            raise InputError("请输入收据编号")
        self.project_no = project_no
        self.receipt_no = receipt_no
        self.directory_url = validate_url(directory_url, label="保证金明细目录网址")
        self._owns_fetcher = fetcher is None
        self.fetcher = fetcher or HttpFetcher()
        self.max_pages = max_pages
        self.max_details = max_details
        mapper = getattr(self.fetcher, "mapper", None)
        if mapper is not None:
            logical_start_url = mapper.to_logical_url(mapper.to_fetch_url(self.directory_url))
        else:
            logical_start_url = self.directory_url
        self.origin = url_origin(logical_start_url)

    def run(self) -> DepositCheckResult:
        try:
            pages, candidates, issues, duplicate_links = self._crawl_directory()
            selected, selection_issues = self._select_candidates(candidates)
            issues.extend(selection_issues)

            records, detail_issues, failed_details, detail_count = self._fetch_details(selected)
            issues.extend(detail_issues)

            now = datetime.now(BEIJING_TZ)
            return resolve_deposit_check(
                project_no=self.project_no,
                receipt_no=self.receipt_no,
                directory_url=self.directory_url,
                records=records,
                issues=issues,
                failed_details=failed_details,
                pages=pages,
                duplicates_skipped=duplicate_links,
                detail_count=detail_count,
                now=now,
            )
        finally:
            if self._owns_fetcher:
                close = getattr(self.fetcher, "close", None)
                if close is not None:
                    close()

    # ------------------------------------------------------------------ crawl
    def _crawl_directory(self) -> tuple[list[str], list[DepositCandidate], list[str], int]:
        queue = [self.directory_url]
        visited_pages: set[str] = set()
        pages: list[str] = []
        candidates_by_url: dict[str, DepositCandidate] = {}
        issues: list[str] = []
        duplicate_links = 0

        while queue:
            if len(pages) >= self.max_pages:
                issues.append(f"分页数量超过上限 {self.max_pages}，未继续抓取")
                break
            page_url = queue.pop(0)
            page_key = canonical_url(page_url)
            if page_key in visited_pages:
                continue
            visited_pages.add(page_key)

            try:
                page = self.fetcher.fetch(page_url)
            except FetchError as exc:
                if not pages:
                    raise ListFetchError(f"无法抓取保证金明细目录：{exc}") from exc
                issues.append(f"分页抓取失败：{page_url}（{exc}）")
                continue

            page_url = page.url
            pages.append(page_url)
            page_candidates, page_links, page_duplicates = self._parse_directory_page(page)
            duplicate_links += page_duplicates

            for candidate in page_candidates:
                key = canonical_url(candidate.url)
                if key in candidates_by_url:
                    if candidate.related:
                        duplicate_links += 1
                    continue
                candidates_by_url[key] = candidate

            for link in page_links:
                if canonical_url(link) not in visited_pages and link not in queue:
                    queue.append(link)

        return pages, list(candidates_by_url.values()), issues, duplicate_links

    def _parse_directory_page(self, page) -> tuple[list[DepositCandidate], list[str], int]:
        soup = page.soup
        candidates: list[DepositCandidate] = []
        page_links: list[str] = []
        duplicates = 0
        seen_on_page: set[str] = set()

        for anchor in soup.find_all("a", href=True):
            href = clean_text(anchor.get("href"))
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            absolute_url = urljoin(page.url, href)
            parsed = urlsplit(absolute_url)
            if parsed.scheme not in {"http", "https"}:
                continue
            absolute_url = urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, "")
            )
            if not same_site(absolute_url, self.origin):
                continue

            text = anchor_text(anchor)
            if is_pagination_link(anchor, absolute_url, text):
                if absolute_url not in page_links:
                    page_links.append(absolute_url)
                continue

            if self._is_site_chrome_link(anchor, absolute_url, text):
                continue
            if self._is_attachment_link(anchor, absolute_url, text):
                continue
            key = canonical_url(absolute_url)
            if key == canonical_url(page.url):
                continue

            container = self._ledger_container(anchor)
            row_text = clean_text(container.get_text(" ", strip=True)) if container else text
            context = " ".join(
                part
                for part in [
                    row_text,
                    self._context_attributes(anchor, container),
                    unquote(absolute_url),
                ]
                if part
            )
            list_time = self._extract_list_time(container, context)
            related = receipt_matches(row_text, self.receipt_no) or receipt_matches(
                text, self.receipt_no
            )

            if key in seen_on_page:
                if related:
                    duplicates += 1
                continue
            seen_on_page.add(key)

            candidates.append(
                DepositCandidate(
                    url=absolute_url,
                    title=text,
                    context=context,
                    found_on=page.url,
                    list_time=list_time,
                    related=related,
                )
            )

        return candidates, page_links, duplicates

    def _ledger_container(self, anchor):
        node = anchor
        for _ in range(8):
            node = node.parent
            if node is None or not getattr(node, "name", None):
                break
            if node.name in {"article", "li", "tr"}:
                return node
            classes = " ".join(node.get("class", [])).lower()
            node_id = str(node.get("id", "")).lower()
            marker = f"{classes} {node_id}"
            if re.search(
                r"(?:deposit|refund|receipt|erratum|ledger|record|item|row|list-item)", marker
            ):
                text_length = len(clean_text(node.get_text(" ", strip=True)))
                if text_length < 1500:
                    return node
        return anchor.parent

    def _context_attributes(self, anchor, container) -> str:
        values: list[str] = []
        nodes = [anchor]
        if container is not None:
            nodes.append(container)
        for node in nodes:
            for attr, label in (
                ("data-receipt-no", "收据编号"),
                ("data-receipt", "收据编号"),
                ("data-project-no", "项目编号"),
                ("data-type", "业务类型"),
                ("data-tx-type", "业务类型"),
                ("data-serial", "流水号"),
            ):
                if not node or not node.has_attr(attr):
                    continue
                value = clean_text(node.get(attr))
                if value:
                    values.append(f"{label}：{value}")
            title = clean_text(node.get("title")) if node else ""
            if title:
                values.append(title)
        return " ".join(values)

    def _extract_list_time(self, container, context: str) -> Optional[datetime]:
        if container is not None:
            for time_tag in container.find_all("time"):
                raw = clean_text(time_tag.get("datetime")) or clean_text(
                    time_tag.get_text(" ", strip=True)
                )
                parsed = parse_datetime(raw)
                if parsed:
                    return parsed
        return extract_labeled_publish_time(context)

    def _is_site_chrome_link(self, anchor, absolute_url: str, text: str) -> bool:
        item_ancestor = anchor.find_parent(["article", "li", "tr"])
        chrome_ancestor = anchor.find_parent(["header", "footer"])
        if chrome_ancestor is not None and item_ancestor is None:
            return True
        normalized_text = clean_text(text).lower()
        if normalized_text in {
            "首页",
            "返回首页",
            "网站首页",
            "公告列表",
            "返回列表",
            "登录",
            "注册",
            "联系我们",
        }:
            return True
        path = urlsplit(absolute_url).path.lower()
        if path in {"/", "/index.html", "/index.htm"} and not receipt_matches(
            text, self.receipt_no
        ):
            return True
        return False

    def _is_attachment_link(self, anchor, absolute_url: str, text: str) -> bool:
        normalized_text = clean_text(text).lower()
        if normalized_text in {
            "附件",
            "附件下载",
            "下载附件",
            "文件下载",
            "查看附件",
            "下载",
        }:
            return True
        classes = " ".join(anchor.get("class", [])).lower()
        if re.search(r"(?:download|attachment|file-download)", classes):
            return True
        path = urlsplit(absolute_url).path.lower()
        if re.search(r"/(?:download|attachment|file)(?:/|$)", path) and "公告" not in normalized_text:
            return True
        return False

    def _select_candidates(
        self, candidates: list[DepositCandidate]
    ) -> tuple[list[DepositCandidate], list[str]]:
        related = [candidate for candidate in candidates if candidate.related]
        # If no directory row names this receipt, still inspect the ledger rows
        # found on the directory; detail pages then verify receipt/project.
        selected = related if related else candidates
        issues: list[str] = []
        if not related:
            issues.append(
                "目录页未找到标注该收据编号的明细行，已检查目录中的明细链接；"
                "如目录分页缺失，结果可能不完整"
            )
        if not selected:
            issues.append("目录页未找到任何保证金明细链接，无法读取到账或退款记录")
        if len(selected) > self.max_details:
            issues.append(f"保证金明细链接超过上限 {self.max_details}，可能存在未核对的明细")
            selected = selected[: self.max_details]
        return selected, issues

    # ------------------------------------------------------------------ detail
    def _fetch_details(
        self, candidates: list[DepositCandidate]
    ) -> tuple[list[DepositRecord], list[str], list[DepositRecord], int]:
        records: list[DepositRecord] = []
        issues: list[str] = []
        failed_details: list[DepositRecord] = []
        detail_count = 0

        for candidate in candidates:
            detail_count += 1
            try:
                page = self.fetcher.fetch(candidate.url)
            except FetchError as exc:
                listed_type = extract_type_from_context(candidate.context)
                guessed_type = (
                    detect_deposit_type(candidate.title, listed_type)
                    or detect_deposit_type(candidate.context)
                )
                if not candidate.related:
                    # A failed page cannot confirm the receipt, so it is never
                    # treated as this receipt's ledger row. Report it anyway so
                    # the operator knows a directory entry could not be read.
                    issues.append(
                        f"保证金明细抓取失败（未能确认收据归属）：{candidate.url}（{exc}）"
                    )
                    continue
                record = DepositRecord(
                    url=candidate.url,
                    title=candidate.title,
                    tx_type=guessed_type or TX_REFUND,
                    serial=None,
                    amount=None,
                    publish_time=candidate.list_time,
                    receipt_no=self.receipt_no,
                    project_no=self.project_no,
                    found_on=candidate.found_on,
                    fetch_ok=False,
                    error=str(exc),
                    source_note="详情页抓取失败，类型/时间来自目录页",
                )
                failed_details.append(record)
                records.append(record)
                continue

            record = self._parse_detail_page(page, candidate)
            if record is not None:
                records.append(record)

        return records, issues, failed_details, detail_count

    def _parse_detail_page(self, page, candidate: DepositCandidate) -> Optional[DepositRecord]:
        soup = page.soup
        content = clean_text(soup.get_text(" ", strip=True))
        title = self._extract_detail_title(soup) or candidate.title
        explicit_type = extract_explicit_type(content)
        tx_type = detect_deposit_type(f"{title} {content}", explicit_type)
        if tx_type is None:
            listed_type = extract_type_from_context(candidate.context)
            tx_type = detect_deposit_type(candidate.title, listed_type)
        if tx_type not in TX_TYPES:
            return None

        declared_receipt = extract_receipt_no(content)
        receipt_here = receipt_matches(f"{title} {content}", self.receipt_no)
        if declared_receipt and not receipt_matches(declared_receipt, self.receipt_no):
            return None
        if not receipt_here and not candidate.related:
            return None

        declared_project = extract_project_no(content)
        if declared_project and normalize_project(declared_project) != normalize_project(
            self.project_no
        ):
            return None

        detail_time = self._extract_detail_publish_time(soup, content)
        publish_time = detail_time or candidate.list_time
        serial = extract_serial(content) or self._serial_from_url(candidate.url)
        if tx_type == TX_ERRATUM:
            amount = extract_erratum_amount(content)
            reference = extract_reference(content)
            original_amount = extract_original_amount(content)
        else:
            amount = extract_amount(content)
            reference = None
            original_amount = None

        source_note = None
        if publish_time is None:
            source_note = "未能从详情页或目录页提取发布时间"
        elif detail_time is None:
            source_note = "发布时间取自目录页"

        return DepositRecord(
            url=page.url,
            title=title,
            tx_type=tx_type,
            serial=serial,
            amount=amount,
            publish_time=publish_time,
            project_no=declared_project or self.project_no,
            receipt_no=self.receipt_no,
            reference=reference,
            original_amount=original_amount,
            found_on=candidate.found_on,
            source_note=source_note,
        )

    def _extract_detail_title(self, soup: BeautifulSoup) -> Optional[str]:
        for selector in ("h1", ".article-title", ".detail-title", ".notice-title", ".title"):
            node = soup.select_one(selector)
            if node:
                text = clean_text(node.get_text(" ", strip=True))
                if text:
                    return text
        if soup.title:
            text = clean_text(soup.title.get_text(" ", strip=True))
            return re.split(r"[-_—|]", text)[0].strip() or text
        return None

    def _extract_detail_publish_time(
        self, soup: BeautifulSoup, content: str
    ) -> Optional[datetime]:
        meta_names = {
            "article:published_time",
            "pubdate",
            "publishdate",
            "publish_date",
            "date",
            "dc.date",
            "dc.date.issued",
        }
        for meta in soup.find_all("meta"):
            name = str(meta.get("property") or meta.get("name") or "").lower()
            if name in meta_names and meta.get("content"):
                parsed = parse_datetime(meta.get("content"))
                if parsed:
                    return parsed
        labelled = extract_labeled_publish_time(content)
        if labelled:
            return labelled
        for time_tag in soup.find_all("time"):
            raw = clean_text(time_tag.get("datetime")) or clean_text(
                time_tag.get_text(" ", strip=True)
            )
            parsed = parse_datetime(raw)
            if not parsed:
                continue
            parent_text = (
                clean_text(time_tag.parent.get_text(" ", strip=True)) if time_tag.parent else ""
            )
            marker = " ".join(time_tag.get("class", [])).lower()
            if re.search(r"(?:发布|时间|publish|date|time)", parent_text + " " + marker, re.I):
                return parsed
        return None

    def _serial_from_url(self, url: str) -> Optional[str]:
        path = urlsplit(url).path
        stem = path.rsplit("/", 1)[-1]
        stem = re.sub(r"\.(?:html?|shtml)$", "", stem, flags=re.I)
        return stem or None
