// Pure helpers for the invoice CSV import. No DOM, no React - so they can be
// reasoned about (and tested) on their own. The browser does the parsing; the
// server re-validates every row, so nothing here is trusted for correctness.

/** RFC-4180-ish parser: quoted fields, escaped quotes, CRLF/LF, BOM. */
export function parseCsv(text: string): string[][] {
  const src = text.replace(/^﻿/, "");
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let inQuotes = false;
  for (let i = 0; i < src.length; i++) {
    const ch = src[i]!;
    if (inQuotes) {
      if (ch === '"') {
        if (src[i + 1] === '"') {
          field += '"';
          i++;
        } else inQuotes = false;
      } else field += ch;
    } else if (ch === '"') inQuotes = true;
    else if (ch === ",") {
      row.push(field);
      field = "";
    } else if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && src[i + 1] === "\n") i++;
      row.push(field);
      field = "";
      if (row.some((c) => c.trim() !== "")) rows.push(row);
      row = [];
    } else field += ch;
  }
  row.push(field);
  if (row.some((c) => c.trim() !== "")) rows.push(row);
  return rows;
}

/** "₹1,25,000.50", "Rs. 5,000", "(500)" -> number | null. Never guesses. */
export function parseAmount(raw: string | undefined): number | null {
  if (raw == null) return null;
  let s = raw.trim();
  if (!s) return null;
  const neg = /^\(.*\)$/.test(s) || s.startsWith("-");
  s = s.replace(/[₹]|rs\.?|inr/gi, "").replace(/[(),\s-]/g, "");
  if (!/^\d+(\.\d+)?$/.test(s)) return null;
  const n = Number(s);
  return neg ? -n : n;
}

const MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];

/**
 * Dates -> "YYYY-MM-DDT00:00:00" or null. Accepts YYYY-MM-DD, DD/MM/YYYY,
 * DD-MM-YYYY, DD.MM.YYYY, "12 Oct 2026", "12-Oct-26". An ambiguous
 * numeric date is read DD/MM (this is India); an impossible date is null.
 */
export function parseDate(raw: string | undefined): string | null {
  if (raw == null) return null;
  const s = raw.trim();
  if (!s) return null;
  let y: number, m: number, d: number;
  let match: RegExpMatchArray | null;
  if ((match = s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})/))) {
    y = +match[1]!;
    m = +match[2]!;
    d = +match[3]!;
  } else if ((match = s.match(/^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2}|\d{4})$/))) {
    d = +match[1]!;
    m = +match[2]!;
    y = +match[3]!;
    if (y < 100) y += 2000;
  } else if ((match = s.match(/^(\d{1,2})[\s\-]([A-Za-z]{3,9})[\s\-,]*(\d{2}|\d{4})$/))) {
    d = +match[1]!;
    m = MONTHS.indexOf(match[2]!.slice(0, 3).toLowerCase()) + 1;
    y = +match[3]!;
    if (y < 100) y += 2000;
  } else return null;
  if (m < 1 || m > 12 || d < 1) return null;
  const dim = new Date(Date.UTC(y, m, 0)).getUTCDate();
  if (d > dim || y < 2000 || y > 2100) return null;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${y}-${pad(m)}-${pad(d)}T00:00:00`;
}

export type ImportRowDraft = {
  line: number; // 1-based line in the file (header = 1)
  invoice_no: string;
  amount_inr: number;
  company_name?: string;
  gstin?: string;
  issued_at?: string;
  due_at?: string;
  /** Problems found while reading the row, before the server sees it. */
  clientProblems: string[];
};

type Field = "invoice_no" | "amount" | "company" | "gstin" | "issued" | "due";

const HEADER_SYNONYMS: Record<Field, string[]> = {
  invoice_no: ["invoice no", "invoice number", "invoice #", "invoice", "inv no", "inv", "bill no", "invoiceno"],
  amount: ["amount", "invoice amount", "total", "total amount", "outstanding", "amount inr", "amount (inr)", "value"],
  company: ["company", "company name", "client", "client name", "customer", "customer name", "party", "party name", "buyer"],
  gstin: ["gstin", "gst no", "gst number", "gst", "gstin/uin"],
  issued: ["invoice date", "issued", "issue date", "date", "issued at", "issued on", "bill date"],
  due: ["due date", "due", "due on", "due at", "payment due", "payment due date"],
};

function normHeader(h: string) {
  return h.toLowerCase().replace(/[_\-]/g, " ").replace(/\s+/g, " ").trim();
}

export type ColumnMap = Partial<Record<Field, number>>;

export function mapHeaders(header: string[]): ColumnMap {
  const map: ColumnMap = {};
  header.forEach((h, idx) => {
    const n = normHeader(h);
    for (const field of Object.keys(HEADER_SYNONYMS) as Field[]) {
      if (map[field] === undefined && HEADER_SYNONYMS[field].includes(n)) map[field] = idx;
    }
  });
  return map;
}

export function rowsFromCsv(text: string): { drafts: ImportRowDraft[]; missingColumns: string[] } {
  const table = parseCsv(text);
  if (table.length === 0) return { drafts: [], missingColumns: ["a header row"] };
  const header = table[0]!;
  const col = mapHeaders(header);
  const missing: string[] = [];
  if (col.invoice_no === undefined) missing.push("Invoice No");
  if (col.amount === undefined) missing.push("Amount");
  if (col.company === undefined && col.gstin === undefined) missing.push("Company or GSTIN");
  if (missing.length) return { drafts: [], missingColumns: missing };

  const drafts: ImportRowDraft[] = [];
  table.slice(1).forEach((cells, i) => {
    const get = (k: keyof ColumnMap) => (col[k] === undefined ? undefined : (cells[col[k]!] ?? "").trim());
    const problems: string[] = [];
    const amount = parseAmount(get("amount"));
    if (amount === null) problems.push("amount_not_a_number");
    const issued = get("issued");
    const due = get("due");
    const issuedAt = issued ? parseDate(issued) : null;
    const dueAt = due ? parseDate(due) : null;
    if (issued && !issuedAt) problems.push("invoice_date_unreadable");
    if (due && !dueAt) problems.push("due_date_unreadable");
    const draft: ImportRowDraft = {
      line: i + 2,
      invoice_no: get("invoice_no") ?? "",
      amount_inr: amount ?? 0,
      clientProblems: problems,
    };
    const company = get("company");
    const gstin = get("gstin");
    if (company) draft.company_name = company;
    if (gstin) draft.gstin = gstin;
    if (issuedAt) draft.issued_at = issuedAt;
    if (dueAt) draft.due_at = dueAt;
    drafts.push(draft);
  });
  return { drafts, missingColumns: [] };
}

export function toCsv(rows: string[][]): string {
  const esc = (v: string) => (/[",\n\r]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v);
  return rows.map((r) => r.map(esc).join(",")).join("\n");
}
