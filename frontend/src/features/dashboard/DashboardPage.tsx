import { useState } from "react";
import { Link } from "@tanstack/react-router";

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

import { OverviewTab } from "./OverviewTab";
import { EventFeedTab } from "./EventFeedTab";
import { RevenueCustomersTab } from "./RevenueCustomersTab";
import { AuditLogTab } from "./AuditLogTab";

type TabKey = "overview" | "events" | "revenue" | "audit";

export function DashboardPage() {
  const [tab, setTab] = useState<TabKey>("overview");

  return (
    <div className="min-h-screen w-full bg-cream text-ink font-body">
      <header className="border-b border-ink/10 bg-cream/80 sticky top-0 z-10">
        <div className="mx-auto max-w-[1320px] px-6 h-16 flex items-center gap-8">
          <Link to="/" className="flex items-center gap-2.5">
            <span className="font-display font-semibold text-lg tracking-tight">Backstop</span>
          </Link>
          <button
            onClick={() => window.open("/store", "_blank")}
            className="px-3 py-1.5 rounded-md text-ink/50 hover:text-ink text-sm"
          >
            Store ↗
          </button>
          <Link
            to="/architecture"
            className="px-3 py-1.5 rounded-md text-ink/50 hover:text-ink text-sm"
          >
            Architecture
          </Link>
          <Link
            to="/tiers"
            className="px-3 py-1.5 rounded-md text-ink/50 hover:text-ink text-sm"
          >
            Tiers
          </Link>
          <div className="ml-auto flex items-center gap-3">
            <span className="hidden sm:flex items-center gap-1.5 text-xs font-medium px-2.5 py-1 rounded-full bg-soft/10 text-soft">
              <span className="size-1.5 rounded-full bg-soft" /> Test Mode
            </span>
            <span className="text-xs text-ink/40">Auto-refresh 5s</span>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-[1320px] px-6 py-8">
        <Tabs value={tab} onValueChange={(v) => setTab(v as TabKey)}>
          <TabsList className="bg-ink/5 mb-6">
            <TabsTrigger value="overview">Overview</TabsTrigger>
            <TabsTrigger value="events">Event Feed</TabsTrigger>
            <TabsTrigger value="revenue">Revenue &amp; Customers</TabsTrigger>
            <TabsTrigger value="audit">Audit Log</TabsTrigger>
          </TabsList>

          <TabsContent value="overview">
            <OverviewTab onOpenAuditLog={() => setTab("audit")} />
          </TabsContent>
          <TabsContent value="events">
            <EventFeedTab />
          </TabsContent>
          <TabsContent value="revenue">
            <RevenueCustomersTab />
          </TabsContent>
          <TabsContent value="audit">
            <AuditLogTab />
          </TabsContent>
        </Tabs>
      </main>
    </div>
  );
}
