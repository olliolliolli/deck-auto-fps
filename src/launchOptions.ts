// Робота з параметрами запуску Steam напряму, без буфера обміну
// (у режимі гри Steam блокує navigator.clipboard).

declare const SteamClient: any;

export const WRAPPER = "~/.local/bin/autofps-launch";
const CMD = "%command%";
// нова форма (~/.local/bin/autofps-launch) і стара (bash …/AutoFPS/bin/autofps)
const WRAPPER_RE = /(?:bash\s+)?\S*(?:autofps-launch|AutoFPS\/bin\/autofps)\s*/g;

export function hasWrapper(options: string): boolean {
  return /autofps-launch|AutoFPS\/bin\/autofps/.test(options);
}

/** Вставляє обгортку перед %command%; змінні середовища та інші обгортки лишаються. */
/** Стара форма (bash …/AutoFPS/bin/autofps) — потребує оновлення. */
export function isOldWrapper(options: string): boolean {
  return hasWrapper(options) && !/autofps-launch/.test(options);
}

// лаунчери Decky LSFG-VK (0.14+ — ~/.lsfg, старіші — ~/lsfg) і MAKO
const LSFG_RE = /(^|\s)(~\/\.lsfg|~\/lsfg|~\/\.local\/bin\/lsfg\S*|\/home\/[^/\s]+\/(?:\.lsfg|lsfg)|\S*mako-(?:run|launch))(?=\s)/;

export function hasLsfg(options: string): boolean {
  return LSFG_RE.test(` ${options} `);
}

/** Вставляє лаунчер перед %command%; змінні середовища та інші обгортки лишаються.
 *  lsfgLauncher — лаунчер окремого плагіна Decky LSFG-VK: додається разом з AutoFPS. */
export function addWrapper(options: string, lsfgLauncher = ""): string {
  let t = isOldWrapper(options) ? removeWrapper(options) : options.trim();
  if (!hasWrapper(t)) {
    const i = t.indexOf(CMD);
    if (i >= 0) t = `${t.slice(0, i)}${WRAPPER} ${t.slice(i)}`.trim();
    // без %command% Steam вважає параметри аргументами гри — зберігаємо їх після
    else t = `${WRAPPER} ${CMD}${t ? " " + t : ""}`;
  }
  if (lsfgLauncher && !hasLsfg(t)) {
    const j = t.indexOf(WRAPPER);
    t = `${t.slice(0, j)}${lsfgLauncher} ${t.slice(j)}`.trim();
  }
  return t;
}

export function removeWrapper(options: string): string {
  const t = options.replace(WRAPPER_RE, "").replace(/\s+/g, " ").trim();
  return t === CMD ? "" : t;
}

export function readLaunchOptions(appid: number): Promise<string> {
  return new Promise((resolve, reject) => {
    let done = false;
    let reg: { unregister?: () => void } | undefined;
    let candidate: string | null = null; // дані без номера гри — лише як запасний варіант
    const finish = (err: Error | null, value = "") => {
      if (done) return;
      done = true;
      window.clearTimeout(timer);
      // колбек може прийти синхронно, до того як reg присвоєно
      window.setTimeout(() => reg?.unregister?.(), 0);
      err ? reject(err) : resolve(value);
    };
    const timer = window.setTimeout(
      () => (candidate !== null ? finish(null, candidate) : finish(new Error("Steam не відповів"))),
      3000,
    );
    try {
      reg = SteamClient.Apps.RegisterForAppDetails(appid, (d: any) => {
        if (!d) return;
        const opts = typeof d.strLaunchOptions === "string" ? d.strLaunchOptions : "";
        const id = Number(d.unAppID);
        if (id === appid) finish(null, opts);
        else if (!id) candidate = opts;
        // дані іншої гри ігноруємо
      });
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
