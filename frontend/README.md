# Revenue Guardian

A FastAPI + PostgreSQL agent that sits on top of Razorpay Test Mode, intercepting three kinds of revenue loss — failed payments, checkout drop-offs, and pre-checkout cart abandonment/cancellation. For each signal it runs a deterministic rules engine (bounded, per-tier discount bands and caps) optionally refined by an LLM, gates low-confidence or risky cases to a human, and executes the resulting action. Every state change is written to an append-only, hash-chained audit log, and every rupee entering an at-risk bucket is tracked through to exactly one exit — recovered, lost, or handed to a human — across three separate threads (soft, declined, failed) so nothing is ever double-counted. Customers are tiered (New/Casual/Regular/Loyal/Risk) by a 0–100 engagement score rather than raw success rate, which drives incentive eligibility and sizing. A merchant dashboard and demo storefront sit on top, including an LLM-powered insights layer that analyzes policy performance and can recommend — and one-click apply — configuration changes.
design build its home page , dashboard page for merchant , store page for users and login page for users accordingly

This project was built with [Lovable](https://lovable.dev).

## Build with Lovable

Continue developing this project in the [Lovable editor](https://lovable.dev/projects/7e5a0675-2760-438a-90d2-67c206d2df77).

- **Ship faster**: describe what you want to build and Lovable handles the code.
- **Stay in sync**: every change made in Lovable is committed straight to this repository.
- **Full ownership**: this code is yours. Push to `main` on GitHub and your changes sync back into Lovable, ready for your next prompt.

## Development

Prefer working locally? You need Node.js and npm — [install with nvm](https://github.com/nvm-sh/nvm#installing-and-updating).

```sh
git clone <this-repository-url>
cd <repository-name>
npm i
npm run dev
```
