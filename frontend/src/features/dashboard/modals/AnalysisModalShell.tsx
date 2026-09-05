import type { ReactNode } from "react";

import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";

export function AnalysisModalShell({
  open,
  onOpenChange,
  title,
  children,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  children: ReactNode;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-4xl max-h-[90vh] overflow-y-auto bg-cream border-ink/10 text-ink font-body">
        <DialogTitle className="font-display text-lg font-bold">{title}</DialogTitle>
        {children}
      </DialogContent>
    </Dialog>
  );
}
