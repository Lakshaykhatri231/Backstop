import { useState } from "react";

import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { tierTone } from "@/lib/constants/tiers";
import type { MerchantCustomer } from "@/lib/api/merchant";
import { ToneBadge } from "./ToneBadge";

const SHOW_LIMIT = 5;

export function CustomersTable({ customers }: { customers: MerchantCustomer[] }) {
  const [expanded, setExpanded] = useState(false);

  if (customers.length === 0) {
    return (
      <div className="text-center py-8 text-ink/40 text-sm">
        No customers yet — run scripts/seed_demo_data.py or register via /store.
      </div>
    );
  }

  const visible = expanded ? customers : customers.slice(0, SHOW_LIMIT);
  const remaining = customers.length - SHOW_LIMIT;

  return (
    <div>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Name</TableHead>
            <TableHead>Tier</TableHead>
            <TableHead>Purchases</TableHead>
            <TableHead>Engagement</TableHead>
            <TableHead>Common failure</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {visible.map((c) => (
            <TableRow key={c.id}>
              <TableCell className="text-xs">
                {c.name} <span className="text-ink/40">({c.email})</span>
              </TableCell>
              <TableCell>
                <ToneBadge label={c.tier} tone={tierTone(c.tier)} />
              </TableCell>
              <TableCell className="text-xs">
                {c.stats.successful_orders}/{c.stats.total_orders}
                {c.stats.total_payment_attempts > c.stats.total_orders && (
                  <span className="text-ink/40"> ({c.stats.total_payment_attempts} tries)</span>
                )}
              </TableCell>
              <TableCell className="text-xs">{c.stats.engagement_score ?? "—"}</TableCell>
              <TableCell className="text-xs text-ink/40">{c.stats.most_common_failure_reason ?? "—"}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>

      {remaining > 0 && (
        <div className="text-center mt-3">
          <button
            onClick={() => setExpanded((e) => !e)}
            className="text-xs font-semibold text-brand hover:underline"
          >
            {expanded ? "Show less" : `Show all ${customers.length} customers (${remaining} more)`}
          </button>
        </div>
      )}
    </div>
  );
}
