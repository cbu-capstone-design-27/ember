"use client";

import { useState, type InputHTMLAttributes } from "react";
import { EyeIcon, EyeOffIcon } from "./icons.tsx";

/** A password field with a show/hide button. */
export function PasswordInput(props: Omit<InputHTMLAttributes<HTMLInputElement>, "type">) {
  const [visible, setVisible] = useState(false);
  return (
    <div className="input-wrap">
      <input {...props} type={visible ? "text" : "password"} className="input" />
      <button
        type="button"
        className="btn btn-ghost btn-icon btn-sm input-action"
        onClick={() => setVisible((v) => !v)}
        aria-label={visible ? "Hide password" : "Show password"}
        aria-pressed={visible}
      >
        {visible ? <EyeOffIcon /> : <EyeIcon />}
      </button>
    </div>
  );
}

/** 0 (too short) to 4 (strong). Length matters most; variety helps. */
export function passwordScore(password: string, minLength = 8): number {
  if (password.length < minLength) return 0;
  let score = 1;
  if (password.length >= 12) score++;
  if (/[a-z]/.test(password) && /[A-Z]/.test(password)) score++;
  if (/\d/.test(password) && /[^A-Za-z0-9]/.test(password)) score++;
  else if (password.length >= 16) score++;
  return Math.min(score, 4);
}

export const SCORE_LABELS = ["Too short", "Weak", "Fair", "Good", "Strong"];
export const SCORE_COLORS = ["var(--surface-3)", "var(--danger)", "var(--warning)", "var(--success)", "var(--success)"];
