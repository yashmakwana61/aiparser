from __future__ import annotations

import io
import re

import pandas as pd

# Mapping of common spreadsheet headers to standard JSON fields.
COLUMN_ALIASES = {
    "product_name": [
        "item",
        "product",
        "description",
        "name",
        "product name",
        "item name",
        "item description",
        "product description",
        "part name",
    ],
    "quantity": ["qty", "quantity", "qty ordered", "quantity ordered", "count"],
    "uom": ["uom", "unit", "units", "unit of measure", "uom name"],
    "unit_price": [
        "rate",
        "unit price",
        "price",
        "price per unit",
        "rate per unit",
        "unit cost",
    ],
}

# Labels that hint at customer information in cells above the item table.
CUSTOMER_LABELS = {
    "customer",
    "customer name",
    "party",
    "party name",
    "bill to",
    "client",
    "buyer",
    "consignee",
}


def _normalize_column(value) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()


def _to_number(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(match.group()) if match else None


class ExcelExtractor:
    @staticmethod
    def read_dataframe(data: bytes) -> pd.DataFrame:
        return pd.read_excel(io.BytesIO(data), sheet_name=0)

    @staticmethod
    def detect_columns(df: pd.DataFrame) -> dict[str, str | None]:
        """Map spreadsheet columns to standard fields using header aliases."""
        mapping: dict[str, str | None] = {field: None for field in COLUMN_ALIASES}
        normalized = [(column, _normalize_column(column)) for column in df.columns]

        for field, aliases in COLUMN_ALIASES.items():
            alias_set = {_normalize_column(alias) for alias in aliases}
            for column, key in normalized:
                if key in alias_set:
                    mapping[field] = column
                    break

        for field, aliases in COLUMN_ALIASES.items():
            if mapping[field]:
                continue
            for column, key in normalized:
                if any(
                    alias and (alias in key or key in alias)
                    for alias in (_normalize_column(a) for a in aliases)
                ):
                    mapping[field] = column
                    break
        return mapping

    @staticmethod
    def extract_items(df: pd.DataFrame, mapping: dict[str, str | None]) -> list[dict]:
        """Convert spreadsheet rows directly into standard item dicts."""
        items: list[dict] = []
        product_col = mapping.get("product_name")
        quantity_col = mapping.get("quantity")
        price_col = mapping.get("unit_price")
        uom_col = mapping.get("uom")

        for _, row in df.iterrows():
            raw_name = row.get(product_col) if product_col else None
            if raw_name is None or pd.isna(raw_name):
                continue
            product_name = str(raw_name).strip()
            if not product_name:
                continue

            quantity = _to_number(row.get(quantity_col)) if quantity_col is not None else None
            unit_price = _to_number(row.get(price_col)) if price_col is not None else None

            uom = "Units"
            if uom_col is not None and row.get(uom_col) is not None and not pd.isna(row.get(uom_col)):
                uom = str(row.get(uom_col)).strip() or "Units"

            items.append(
                {
                    "product_name": product_name,
                    "quantity": quantity or 0,
                    "unit_price": unit_price,
                    "uom": uom,
                }
            )
        return items

    @staticmethod
    def to_text_preview(df: pd.DataFrame, max_rows: int = 50) -> str:
        """Serialize the first rows into plain text for the AI parser."""
        lines = [" | ".join(str(column) for column in df.columns)]
        for row in df.head(max_rows).itertuples(index=False):
            lines.append(" | ".join("" if pd.isna(v) else str(v) for v in row))
        return "\n".join(lines)

    @staticmethod
    def extract_preview(data: bytes, max_rows: int = 50) -> str:
        """Read a workbook from bytes and produce a text preview ('' on failure)."""
        try:
            df = ExcelExtractor.read_dataframe(data)
        except Exception:
            return ""
        if df.empty:
            return ""
        return ExcelExtractor.to_text_preview(df, max_rows=max_rows)

    # ---------------------------------------------- structured layer (Phase 4)

    @staticmethod
    def read_workbook(data: bytes) -> dict[str, pd.DataFrame]:
        """Read every sheet with no header assumption (header row = None)."""
        return pd.read_excel(io.BytesIO(data), sheet_name=None, header=None)

    @staticmethod
    def _row_cells(row: pd.Series) -> list[str]:
        cells: list[str] = []
        for value in row:
            if value is None or (isinstance(value, float) and pd.isna(value)):
                continue
            text = str(value).strip()
            if text:
                cells.append(_normalize_column(text))
        return cells

    @staticmethod
    def find_header_row(raw_df: pd.DataFrame, scan_rows: int = 15) -> int | None:
        """Locate the header row without assuming row 1.

        A candidate header is the first scanned row containing both a
        product-name alias and a quantity alias.
        """
        product_aliases = {_normalize_column(a) for a in COLUMN_ALIASES["product_name"]}
        quantity_aliases = {_normalize_column(a) for a in COLUMN_ALIASES["quantity"]}
        for index in range(min(scan_rows, len(raw_df))):
            cells = set(ExcelExtractor._row_cells(raw_df.iloc[index]))
            if not cells:
                continue
            has_product = any(alias in cells for alias in product_aliases)
            has_quantity = any(
                alias in cells or any(alias in cell for cell in cells if cell) for alias in quantity_aliases
            )
            if has_product and has_quantity:
                return index
        return None

    @staticmethod
    def extract_customer_hint(raw_df: pd.DataFrame, header_row: int | None, scan_rows: int = 15) -> str:
        """Scan cells above the table for an explicit 'Customer:' label."""
        limit = header_row if header_row is not None else min(scan_rows, len(raw_df))
        for index in range(min(limit, len(raw_df))):
            values = list(raw_df.iloc[index])
            for position, value in enumerate(values):
                if value is None or (isinstance(value, float) and pd.isna(value)):
                    continue
                normalized = _normalize_column(str(value))
                if normalized in CUSTOMER_LABELS:
                    for candidate in values[position + 1 :]:
                        if candidate is None or (isinstance(candidate, float) and pd.isna(candidate)):
                            continue
                        text = str(candidate).strip()
                        if text:
                            return text
                        break
        return ""

    @staticmethod
    def extract_customer_info(raw_df: pd.DataFrame, header_row: int | None, scan_rows: int = 15) -> dict:
        """Labeled hints win; otherwise the single unambiguous free-text cell
        above the table is offered as a candidate. Multiple candidates are
        reported but never silently chosen."""
        labeled = ExcelExtractor.extract_customer_hint(raw_df, header_row, scan_rows=scan_rows)
        if labeled:
            return {"hint": labeled, "candidates": [labeled]}
        known_aliases: set[str] = set(CUSTOMER_LABELS)
        for aliases in COLUMN_ALIASES.values():
            known_aliases.update(_normalize_column(a) for a in aliases)
        limit = header_row if header_row is not None else min(scan_rows, len(raw_df))
        candidates: list[str] = []
        for index in range(min(limit, len(raw_df))):
            for value in list(raw_df.iloc[index]):
                if value is None or (isinstance(value, float) and pd.isna(value)):
                    continue
                text = str(value).strip()
                if len(text) < 3 or not any(ch.isalpha() for ch in text):
                    continue
                normalized = _normalize_column(text)
                if normalized in known_aliases or normalized.replace(" ", "").isdigit():
                    continue
                if text not in candidates:
                    candidates.append(text)
        hint = candidates[0] if len(candidates) == 1 else ""
        return {"hint": hint, "candidates": candidates}

    @staticmethod
    def _table_from_header(raw_df: pd.DataFrame, header_row: int) -> pd.DataFrame | None:
        header = [str(v).strip() if not pd.isna(v) else f"col_{i}" for i, v in enumerate(raw_df.iloc[header_row])]
        body = raw_df.iloc[header_row + 1 :].reset_index(drop=True)
        body.columns = header
        body = body.dropna(how="all").reset_index(drop=True)
        if body.empty:
            return None
        return body

    @staticmethod
    def extract_from_workbook(data: bytes, scan_rows: int = 15) -> tuple[list[dict], dict]:
        """Inspect ALL sheets; aggregate items from every sheet whose header
        can be located. Returns ``(items, info)`` where ``info`` carries the
        per-sheet outcome and any customer hint found above the tables."""
        items: list[dict] = []
        sheets_info: list[dict] = []
        customer_hint = ""
        candidate_hints: list[str] = []
        try:
            workbook = ExcelExtractor.read_workbook(data)
        except Exception:
            return items, {"sheets": [], "customer_hint": "", "customer_hints": [], "readable": False}

        for name, raw_df in workbook.items():
            # Drop fully-blank columns only; row positions stay true to the
            # sheet so header_row indices are meaningful to operators.
            raw_df = raw_df.dropna(axis=1, how="all").reset_index(drop=True)
            entry: dict = {"sheet": name, "items": 0}
            try:
                header_row = ExcelExtractor.find_header_row(raw_df, scan_rows=scan_rows)
                info = ExcelExtractor.extract_customer_info(raw_df, header_row, scan_rows=scan_rows)
                if info["hint"] and not customer_hint:
                    customer_hint = info["hint"]
                for candidate in info["candidates"]:
                    if candidate not in candidate_hints:
                        candidate_hints.append(candidate)
                entry["header_row"] = header_row
                if header_row is None:
                    entry["status"] = "no_header"
                    sheets_info.append(entry)
                    continue
                table = ExcelExtractor._table_from_header(raw_df, header_row)
                if table is None:
                    entry["status"] = "empty"
                    sheets_info.append(entry)
                    continue
                mapping = ExcelExtractor.detect_columns(table)
                if not (mapping.get("product_name") and mapping.get("quantity")):
                    entry["status"] = "unmapped_columns"
                    sheets_info.append(entry)
                    continue
                sheet_items = ExcelExtractor.extract_items(table, mapping)
                items.extend(sheet_items)
                entry.update({"status": "extracted", "items": len(sheet_items)})
            except Exception as exc:  # a broken sheet must not sink the file
                entry["status"] = f"error:{exc}"
            sheets_info.append(entry)

        return items, {
            "sheets": sheets_info,
            "customer_hint": customer_hint,
            "customer_hints": candidate_hints,
            "readable": True,
        }

    @staticmethod
    def extract_preview_all_sheets(data: bytes, max_rows_per_sheet: int = 30) -> str:
        """Text preview across every readable sheet (for AI interpretation)."""
        blocks: list[str] = []
        try:
            workbook = ExcelExtractor.read_workbook(data)
        except Exception:
            return ""
        for name, raw_df in workbook.items():
            cleaned = raw_df.dropna(axis=1, how="all").dropna(how="all").reset_index(drop=True)
            if cleaned.empty:
                continue
            lines = [f"[sheet: {name}]"]
            for _, row in cleaned.head(max_rows_per_sheet).iterrows():
                cells = [
                    "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v).strip()
                    for v in row
                ]
                line = " | ".join(cells).rstrip(" |")
                if line.strip():
                    lines.append(line)
            if len(lines) > 1:
                blocks.append("\n".join(lines))
        return "\n\n".join(blocks)
