// Better Auth error codes, in words a person can act on.

const MESSAGES: Record<string, string> = {
  USER_ALREADY_EXISTS: "An account with this email already exists.",
  USER_ALREADY_EXISTS_USE_ANOTHER_EMAIL: "An account with this email already exists.",
  INVALID_EMAIL_OR_PASSWORD: "That email and password don't match.",
  INVALID_EMAIL: "Enter a valid email address.",
  PASSWORD_TOO_SHORT: "Use at least 8 characters for your password.",
  PASSWORD_TOO_LONG: "That password is too long.",
  INVALID_PASSWORD: "Your current password isn't right.",
  CREDENTIAL_ACCOUNT_NOT_FOUND: "This account doesn't use a password.",
};

export function authErrorMessage(
  error: { code?: string; message?: string; status?: number } | null | undefined,
  fallback: string,
): string {
  if (!error) return fallback;
  if (error.status === 429) return "Too many attempts. Wait a minute and try again.";
  return (error.code && MESSAGES[error.code]) || error.message || fallback;
}
