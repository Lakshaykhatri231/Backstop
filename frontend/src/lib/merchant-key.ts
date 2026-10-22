// The shared merchant key (see app/merchant_auth.py), kept in localStorage.
// It only matters when the server has MERCHANT_API_KEY set; otherwise the
// header is simply ignored. Every access is wrapped: storage can be blocked.

const KEY = "backstop.merchantKey";

export function getMerchantKey(): string {
  try {
    return window.localStorage.getItem(KEY) ?? "";
  } catch {
    return "";
  }
}

export function setMerchantKey(value: string): void {
  try {
    window.localStorage.setItem(KEY, value);
  } catch {
    /* storage unavailable - the key just won't persist */
  }
}

export function clearMerchantKey(): void {
  try {
    window.localStorage.removeItem(KEY);
  } catch {
    /* ignore */
  }
}
