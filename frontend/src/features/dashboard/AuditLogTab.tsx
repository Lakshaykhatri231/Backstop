import { useState } from "react";
import { useMutation } from "@tanstack/react-query";

import { getAuditVerify, type AuditVerifyResult } from "@/lib/api/outcomes";
import { useAuditLog } from "@/lib/hooks/useDashboardData";

import { AuditLogTable } from "./components/AuditLogTable";
import { IncentiveAnalysisModal } from "./modals/IncentiveAnalysisModal";
import { RecoveryAnalysisModal } from "./modals/RecoveryAnalysisModal";
import { TierAnalysisModal } from "./modals/TierAnalysisModal";

export function AuditLogTab() {
  const auditLogQ = useAuditLog(100);
  const [showIncentive, setShowIncentive] = useState(false);
  const [showRecovery, setShowRecovery] = useState(false);
  const [showTier, setShowTier] = useState(false);
  const [chain, setChain] = useState<AuditVerifyResult | null>(null);
  const verifyMutation = useMutation({ mutationFn: getAuditVerify, onSuccess: setChain });

  return (
    <div>
      <div className="flex items-center gap-3 mb-5 flex-wrap">
        <h1 className="font-display text-2xl font-semibold tracking-tight">Audit Log</h1>
        <button onClick={() => setShowIncentive(true)} className="rounded-md px-3.5 py-1.5 text-xs font-semibold bg-soft/15 text-soft">
          📊 Incentive Analysis
        </button>
        <button onClick={() => setShowRecovery(true)} className="rounded-md px-3.5 py-1.5 text-xs font-semibold bg-failed/15 text-failed">
          📉 Loss &amp; Recovery Analysis
        </button>
        <button onClick={() => setShowTier(true)} className="rounded-md px-3.5 py-1.5 text-xs font-semibold bg-loyal/15 text-loyal">
          🎯 Tier Analysis
        </button>
        <button
          onClick={() => verifyMutation.mutate()}
          disabled={verifyMutation.isPending}
          className="rounded-md px-3.5 py-1.5 text-xs font-semibold bg-indigo/15 text-indigo disabled:opacity-50"
        >
          {verifyMutation.isPending ? "Verifying..." : "🔐 Verify Chain"}
        </button>
        {chain && (
          <span className={`text-xs font-semibold px-2.5 py-1 rounded-full ${chain.chain_intact ? "bg-soft/15 text-soft" : "bg-failed/15 text-failed"}`}>
            {chain.chain_intact ? "✅" : "❌"} {chain.message}
          </span>
        )}
      </div>

      <IncentiveAnalysisModal open={showIncentive} onOpenChange={setShowIncentive} />
      <RecoveryAnalysisModal open={showRecovery} onOpenChange={setShowRecovery} />
      <TierAnalysisModal open={showTier} onOpenChange={setShowTier} />

      <div className="rounded-2xl bg-white border border-ink/5 p-6 shadow-[0_20px_40px_-30px_rgba(234,88,12,0.4)]">
        {auditLogQ.data ? <AuditLogTable log={auditLogQ.data} /> : <div className="text-ink/40 text-sm py-10 text-center">Loading…</div>}
      </div>
    </div>
  );
}
