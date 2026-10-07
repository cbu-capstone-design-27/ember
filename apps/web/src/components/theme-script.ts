// Inline <head> script that applies the saved theme before first paint, so
// pages never flash the wrong one. Kept apart from theme.tsx because a
// server layout can't read plain values out of a "use client" module.

export const THEME_KEY = "ember-theme";

export const THEME_SCRIPT = `(function(){try{var c=localStorage.getItem("${THEME_KEY}");var d=c==="dark"||(c!=="light"&&matchMedia("(prefers-color-scheme: dark)").matches);var r=document.documentElement;r.dataset.theme=d?"dark":"light";r.style.colorScheme=d?"dark":"light"}catch(e){document.documentElement.dataset.theme="light"}})()`;
