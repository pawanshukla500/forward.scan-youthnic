export type ThemeChoice = "system" | "light" | "dark";

const KEY = "fs_theme";

export function getTheme(): ThemeChoice {
  try {
    const v = localStorage.getItem(KEY);
    return v === "light" || v === "dark" ? v : "system";
  } catch {
    return "system";
  }
}

/** Applies the theme: "system" removes the override so prefers-color-scheme decides. */
export function applyTheme(choice: ThemeChoice = getTheme()) {
  const root = document.documentElement;
  if (choice === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", choice);
}

export function setTheme(choice: ThemeChoice) {
  try {
    if (choice === "system") localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, choice);
  } catch {
    /* storage may be blocked - the choice still applies for this session */
  }
  applyTheme(choice);
}
