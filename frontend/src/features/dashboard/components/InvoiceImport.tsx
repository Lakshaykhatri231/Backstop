import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { ApiError } from "@/lib/api/client";
import {
  commitInvoiceImport,
  IMPORT_ROW_CAP,
  previewInvoiceImport,
  type ImportPreview,
  type ImportResult,
  type ImportRow,
} from "@/lib/api/receivables";
import { rowsFromCsv, toCsv, type ImportRowDraft } from "@/lib/csv";

import { ToneBadge } from "./ToneBadge";

const PROBLEM_TEXT: Record<string, string> = {
  amount_not_a_number: "Amount isn't a number",
  amount_must_be_positive: "Amount must be above zero",
  invoice_date_unreadable: "Invoice date unreadable",
  due_date_unreadable: "Due date unreadable",
  missing_invoice_no: "Missing invoice number",
  invalid_gstin: "GSTIN isn't valid",
  no_company_or_gstin: "No company name or GSTIN",
  ambiguous_company_name: "Company name matches more than one account",
  duplicate_in_file: "Duplicate invoice number in this file",
  due_before_issued: "Due date is before the invoice date",
  account_not_found_and_creation_not_confirmed: "Company not in accounts (new accounts not confirmed)",
};

const problemText = (p: string) => PROBLEM_TEXT[p] ?? p.replace(/_/g, " ");

type Line = {
  line: number;
  invoice_no: string;
  company: string;
  amount_inr: number;
  status: string; // new | exists | rejected | duplicate_in_file | created
  problems: string[];
  needsNewAccount: boolean;
};

function download(name: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/csv" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

function errText(e: unknown) {
  if (e instanceof ApiError) {
    const d = (e.body as { detail?: unknown } | null)?.detail;
    if (typeof d === "string") return d;
  }
  return e instanceof Error ? e.message : "Import failed";
}

export function InvoiceImport() {
  const qc = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const [drafts, setDrafts] = useState<ImportRowDraft[]>([]);
  const [sent, setSent] = useState<ImportRowDraft[]>([]);
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [result, setResult] = useState<ImportResult | null>(null);
  const [createAccounts, setCreateAccounts] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fileName, setFileName] = useState("");

  const previewM = useMutation({
    mutationFn: (rows: ImportRow[]) => previewInvoiceImport(rows),
    onSuccess: setPreview,
    onError: (e) => setError(errText(e)),
  });
  const commitM = useMutation({
    mutationFn: (rows: ImportRow[]) => commitInvoiceImport(rows, createAccounts),
    onSuccess: (r) => {
      setResult(r);
      qc.invalidateQueries({ queryKey: ["receivables"] });
      qc.invalidateQueries({ queryKey: ["contacts"] });
      qc.invalidateQueries({ queryKey: ["merchant"] });
    },
    onError: (e) => setError(errText(e)),
  });

  function reset() {
    setDrafts([]);
    setSent([]);
    setPreview(null);
    setResult(null);
    setCreateAccounts(false);
    setError(null);
    setFileName("");
    if (fileRef.current) fileRef.current.value = "";
  }

  async function onFile(file: File | undefined) {
    if (!file) return;
    reset();
    setFileName(file.name);
    const { drafts: parsed, missingColumns } = rowsFromCsv(await file.text());
    if (missingColumns.length) {
      setError(`Couldn't find these columns: ${missingColumns.join(", ")}. Expected headers like Company, GSTIN, Invoice No, Amount, Invoice Date, Due Date.`);
      return;
    }
    if (parsed.length === 0) return setError("The file has no rows.");
    if (parsed.length > IMPORT_ROW_CAP) {
      return setError(`This file has ${parsed.length} rows; at most ${IMPORT_ROW_CAP} can be imported at once. Please split it.`);
    }
    setDrafts(parsed);
    // Rows the browser could not even read never go to the server.
    const sendable = parsed.filter((d) => d.clientProblems.length === 0);
    setSent(sendable);
    if (sendable.length) previewM.mutate(sendable.map(toRow));
    else setPreview({ rows: [], counts: { new: 0, exists: 0, rejected: 0, duplicate_in_file: 0 }, new_accounts: [], total: 0 });
  }

  const toRow = (d: ImportRowDraft): ImportRow => ({
    invoice_no: d.invoice_no,
    amount_inr: d.amount_inr,
    ...(d.company_name ? { company_name: d.company_name } : {}),
    ...(d.gstin ? { gstin: d.gstin } : {}),
    ...(d.issued_at ? { issued_at: d.issued_at } : {}),
    ...(d.due_at ? { due_at: d.due_at } : {}),
  });

  // File order, merging client-side rejects with the server's verdicts.
  const lines: Line[] = (() => {
    if (!preview) return [];
    const serverByLine = new Map<number, ImportPreview["rows"][number]>();
    sent.forEach((d, i) => preview.rows[i] && serverByLine.set(d.line, preview.rows[i]!));
    const resultByLine = new Map<number, ImportResult["results"][number]>();
    if (result) sent.forEach((d, i) => result.results[i] && resultByLine.set(d.line, result.results[i]!));
    return drafts.map((d) => {
      if (d.clientProblems.length) {
        return { line: d.line, invoice_no: d.invoice_no, company: d.company_name ?? d.gstin ?? "", amount_inr: d.amount_inr,
          status: "rejected", problems: d.clientProblems, needsNewAccount: false };
      }
      const s = serverByLine.get(d.line);
      const r = resultByLine.get(d.line);
      return {
        line: d.line, invoice_no: d.invoice_no, company: s?.company_name ?? d.company_name ?? d.gstin ?? "",
        amount_inr: d.amount_inr, status: r?.status ?? s?.status ?? "…",
        problems: r?.problems?.length ? r.problems : (s?.problems ?? []),
        needsNewAccount: s?.account === "new_account",
      };
    });
  })();

  const clientRejected = drafts.filter((d) => d.clientProblems.length).length;
  const counts = preview?.counts;
  const newRows = counts?.new ?? 0;
  const rowsNeedingNewAccount = lines.filter((l) => l.status === "new" && l.needsNewAccount).length;
  const willCreate = result ? 0 : newRows - (createAccounts ? 0 : rowsNeedingNewAccount);

  function downloadRejected() {
    const rows: string[][] = [["line", "invoice_no", "company", "amount", "reason"]];
    lines.filter((l) => l.status === "rejected" || l.status === "duplicate_in_file").forEach((l) =>
      rows.push([String(l.line), l.invoice_no, l.company, String(l.amount_inr), l.problems.map(problemText).join("; ")]));
    download("rejected-invoices.csv", toCsv(rows));
  }

  const tone = (s: string) =>
    s === "new" || s === "created" ? "soft" : s === "exists" ? "neutral" : s === "rejected" ? "failed" : "declined";

  return (
    <div>
      <div className="flex items-center gap-3 flex-wrap">
        <input ref={fileRef} type="file" accept=".csv,text/csv" onChange={(e) => onFile(e.target.files?.[0])}
               className="text-xs" />
        {fileName && (
          <button type="button" onClick={reset} className="text-xs text-ink/50 underline">
            Clear
          </button>
        )}
      </div>
      <p className="text-[11px] text-ink/40 mt-1.5">
        Columns: Company (or GSTIN), Invoice No, Amount, Invoice Date, Due Date. Dates are read day/month/year. Up to{" "}
        {IMPORT_ROW_CAP} rows. Nothing is created until you confirm.
      </p>

      {error && <p className="text-sm text-failed mt-3">{error}</p>}
      {previewM.isPending && <p className="text-sm text-ink/50 mt-3">Checking the file…</p>}

      {preview && counts && (
        <div className="mt-4">
          <p className="text-sm font-semibold">
            {result ? "Imported: " : "Preview: "}
            <span className="text-soft">{result ? result.results.filter((r) => r.status === "created").length : counts.new} new</span>
            {" · "}
            {counts.exists} already exist
            {" · "}
            {counts.duplicate_in_file} duplicate{counts.duplicate_in_file === 1 ? "" : "s"} in file
            {" · "}
            <span className="text-failed">{counts.rejected + clientRejected} rejected</span>
          </p>

          {!result && preview.new_accounts.length > 0 && (
            <label className="flex items-start gap-2 text-xs mt-3 bg-declined/10 rounded-md px-3 py-2">
              <input type="checkbox" className="mt-0.5" checked={createAccounts}
                     onChange={(e) => setCreateAccounts(e.target.checked)} />
              <span>
                <strong>{preview.new_accounts.length} compan{preview.new_accounts.length === 1 ? "y isn't" : "ies aren't"} in your accounts yet:</strong>{" "}
                {preview.new_accounts.join(", ")}. Tick to create {preview.new_accounts.length === 1 ? "it" : "them"}; otherwise
                those {rowsNeedingNewAccount} row{rowsNeedingNewAccount === 1 ? "" : "s"} are skipped. (Check the spelling - a typo creates a duplicate client.)
              </span>
            </label>
          )}

          <div className="overflow-x-auto mt-3 max-h-72 overflow-y-auto border border-ink/10 rounded-lg">
            <table className="w-full text-left text-xs">
              <thead className="sticky top-0 bg-cream">
                <tr className="text-ink/40">
                  {["Line", "Invoice", "Company", "Amount", "Status", "Notes"].map((h) => (
                    <th key={h} className="px-2 py-1.5 font-semibold">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {lines.map((l) => (
                  <tr key={l.line} className="border-t border-ink/10">
                    <td className="px-2 py-1 text-ink/40">{l.line}</td>
                    <td className="px-2 py-1">{l.invoice_no || "—"}</td>
                    <td className="px-2 py-1">
                      {l.company || "—"}
                      {l.needsNewAccount && l.status === "new" && <span className="ml-1 text-declined">(new)</span>}
                    </td>
                    <td className="px-2 py-1 tabular-nums">₹{Math.round(l.amount_inr).toLocaleString("en-IN")}</td>
                    <td className="px-2 py-1"><ToneBadge label={l.status.replace(/_/g, " ")} tone={tone(l.status)} /></td>
                    <td className="px-2 py-1 text-ink/50">{l.problems.map(problemText).join("; ")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="flex items-center gap-3 mt-3 flex-wrap">
            {!result && (
              <button
                type="button"
                disabled={commitM.isPending || willCreate <= 0}
                onClick={() => commitM.mutate(sent.map(toRow))}
                className="rounded-md px-4 py-2 text-xs font-bold bg-ink text-cream disabled:opacity-50"
              >
                {commitM.isPending ? "Importing…" : `Confirm import (${Math.max(0, willCreate)} invoice${willCreate === 1 ? "" : "s"})`}
              </button>
            )}
            {(counts.rejected + clientRejected + counts.duplicate_in_file > 0) && (
              <button type="button" onClick={downloadRejected} className="text-xs underline text-ink/60">
                Download rejected rows
              </button>
            )}
            {result && result.accounts_created.length > 0 && (
              <span className="text-xs text-ink/60">Created accounts: {result.accounts_created.join(", ")}</span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
