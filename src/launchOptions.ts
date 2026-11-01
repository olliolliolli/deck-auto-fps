// Робота з параметрами запуску Steam напряму, без буфера обміну
// (у режимі гри Steam блокує navigator.clipboard).

declare const SteamClient: any;

export const WRAPPER = "bash ~/homebrew/plugins/AutoFPS/bin/autofps";
const CMD = "%command%";
const WRAPPER_RE = /(?:bash\s+)?\S*AutoFPS\/bin\/autofps\s*/g;

export function hasWrapper(options: string): boolean {
  return /AutoFPS\/bin\/autofps/.test(options);
}

/** Вставляє обгортку перед %command%; змінні середовища та інші обгортки лишаються. */
export function addWrapper(options: string): string {
  const t = options.trim();
  if (hasWrapper(t)) return t;
  const i = t.indexOf(CMD);
  if (i >= 0) return `${t.slice(0, i)}${WRAPPER} ${t.slice(i)}`.trim();
  // без %command% Steam вважає параметри аргументами гри — зберігаємо їх після
  return `${WRAPPER} ${CMD}${t ? " " + t : ""}`;
}

export function removeWrapper(options: string): string {
  const t = options.replace(WRAPPER_RE, "").replace(/\s+/g, " ").trim();
  return t === CMD ? "" : t;
}

export function readLaunchOptions(appid: number): Promise<string> {
  return new Promise((resolve, reject) => {
    let done = false;
    let reg: { unregister?: () => void } | undefined;
    const finish = (err: Error | null, value = "") => {
      if (done) return;
      done = true;
      window.clearTimeout(timer);
      // колбек може прийти синхронно, до того як reg присвоєно
      window.setTimeout(() => reg?.unregister?.(), 0);
      err ? reject(err) : resolve(value);
    };
    const timer = window.setTimeout(() => finish(new Error("Steam не відповів")), 5000);
    try {
      reg = SteamClient.Apps.RegisterForAppDetails(appid, (d: any) =>
        finish(null, d?.strLaunchOptions ?? ""),
      );
    } catch (e) {
      finish(e instanceof Error ? e : new Error(String(e)));
    }
  });
}

export async function writeLaunchOptions(appid: number, value: string): Promise<boolean> {
  await Promise.resolve(SteamClient.Apps.SetAppLaunchOptions(appid, value));
  // перевіряємо, що Steam справді зберіг
  for (let i = 0; i < 10; i++) {
    await new Promise((r) => window.setTimeout(r, 150));
    try {
      if ((await readLaunchOptions(appid)).trim() === value.trim()) return true;
    } catch {
      /* пробуємо ще */
    }
  }
  return false;
}

/** Копіювання, яке працює в CEF Steam (запасний варіант). */
export function copyText(text: string): boolean {
  try {
    const el = document.createElement("textarea");
    el.value = text;
    el.setAttribute("readonly", "");
    el.style.position = "fixed";
    el.style.opacity = "0";
    document.body.appendChild(el);
    el.focus();
    el.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(el);
    if (ok) return true;
  } catch {
    /* падаємо на clipboard API */
  }
  try {
    navigator.clipboard?.writeText(text).catch(() => undefined);
  } catch {
    /* немає доступу */
  }
  return false;
}
