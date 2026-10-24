// Shared props for the brand logos in this folder. Every logo is decorative
// (aria-hidden); put the name in visible text or an aria-label next to it.

export interface LogoProps {
  /** Width and height in px. */
  size?: number;
  className?: string;
}

/** An id that is safe inside url(#…) references (React's useId has colons). */
export function svgId(reactId: string, name: string): string {
  return `${name}-${reactId.replace(/[^a-zA-Z0-9_-]/g, "")}`;
}
