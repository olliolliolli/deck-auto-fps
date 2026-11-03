import {
  ButtonItem,
  DialogButton,
  DropdownItem,
  Focusable,
  Navigation,
  Field,
  PanelSection,
  PanelSectionRow,
  Router,
  SliderField,
  ToggleField,
  staticClasses,
} from "@decky/ui";
import {
  addEventListener,
  callable,
  definePlugin,
  openFilePicker,
  removeEventListener,
  toaster,
} from "@decky/api";
import { memo, useEffect, useMemo, useState } from "react";
import { FaTachometerAlt } from "react-icons/fa";
import {
  addWrapper,
  hasLsfg,
  hasWrapper,
  isOldWrapper,
  readLaunchOptions,
  removeWrapper,
  writeLaunchOptions,
} from "./launchOptions";

type Settings = {
  enabled: boolean;
  mode: string;
  headroom: number;
  drop_ratio: number;
  warmup_s: number;
  probe_s: number;
  fg_mode: "external" | "off" | "auto" | "always";
  fg_tool?: "lsfg" | "mako" | "none";
  ue5_tweak?: boolean;
  unity_tweak?: boolean;
  auto_tdp?: "battery" | "always" | "off";
  auto_gpu_clock?: boolean;
  mango_limiter?: boolean;
  power_profiles?: boolean;
  bat_mode?: string;
  ac_mode?: string;
  community_profiles?: boolean;
  fg_min_base: number;
  fg_allow_3x: boolean;
  fg_flow_scale: number;
  fg_performance_mode: boolean;
  fps_counts_generated: boolean;
  autotune: boolean;
  quality_target: string;
  respect_manual_graphics: boolean;
  start_on_button: boolean;
  lowres_for_quality: boolean;
  opti_upscaler: string;
  opti_method: string;
  opti_gpu_arch: string;
  opti_fg: boolean;
  ref_sharpness: string;
  bridge_version: string;
};

type Tune = {
  appid: string;
  engine: string | null;
  state: "tuning" | "done" | "cpu" | "off" | "wait" | "user";
  level: number | null;
  steps: number | null;
  note: string;
  summary: string;
  target: number | null;
};

type Status = {
  mango_cap?: number;
  lsfg_launcher: string;
  fg_launchers?: { lsfg?: string; mako?: string };
  device: {
    id: string;
    label: string;
    tiers: number[];
    max_hz: number;
    quality_target: number;
    res: [number, number];
    vrr?: boolean;
    vrr_range?: [number, number] | null;
  };
  settings: Settings;
  appid: string | null;
  phase: string;
  base: number;
  mult: number;
  out: number;
  fps_now: number | null;
  native_p10: number;
  message: string;
  lsfg: { ready: boolean; hint: string; layers: string[]; plugins: string[]; dll: string };
  tune: Tune | null;
  events: { t: number; title: string; body: string }[];
  launch_option: string;
  power?: {
    ac: boolean;
    battery: number | null;
    tdp_available: boolean;
    kind: string | null;
    level: string | null;
    levels: number;
    level_i: number | null;
    tdp_state: string;
    telemetry?: { temp: number | null; fan: number | null; watts: number | null };
    hot?: boolean;
    tdp_error: string;
    mode: string;
    gpu?: { available: boolean; state: string; clock: number | null; min: number | null; max: number | null; error: string };
  };
  game_update?: { appid: string; old: string; new: string; t: number } | null;
  graphics_target?: number;
};

const TDP_STATE: Record<string, string> = {
  off: "не керую",
  max: "повна потужність",
  descending: "шукаю найтихіше…",
  settled: "підібрано",
};

const shareProfile = callable<
  [appid?: string],
  { ok: boolean; url?: string; error?: string; profile?: { name: string; graphics?: { summary?: string } } }
>("share_profile");

const TABS = [
  { id: "game", label: "Гра" },
  { id: "fps", label: "FPS" },
  { id: "gfx", label: "Графіка" },
  { id: "fg", label: "FG" },
  { id: "more", label: "Ще" },
];
let lastTab = "game";

function TabBar({ tab, setTab }: { tab: string; setTab: (t: string) => void }) {
  return (
    <Focusable style={{ display: "flex", gap: "4px", padding: "0 0 8px 0" }} flow-children="horizontal">
      {TABS.map((t) => (
        <DialogButton
          key={t.id}
          onClick={() => setTab(t.id)}
          style={{
            flex: 1,
            minWidth: 0,
            padding: "6px 2px",
            fontSize: "12px",
            lineHeight: "16px",
            opacity: tab === t.id ? 1 : 0.55,
            borderBottom: tab === t.id ? "2px solid #1a9fff" : "2px solid transparent",
          }}
        >
          {t.label}
        </DialogButton>
      ))}
    </Focusable>
  );
}

const getStatus = callable<[], Status>("get_status");
const setSetting = callable<[key: string, value: unknown], boolean>("set_setting");
const reprobe = callable<[], boolean>("reprobe");
const forgetAll = callable<[], boolean>("forget_all");
const retune = callable<[appid?: string], { ok: boolean; message: string }>("retune");
const listGames = callable<[], Game[]>("list_games");
const collectDiagnostics = callable<
  [],
  { ok: boolean; path?: string; size?: number; error?: string }
>("collect_diagnostics");
const setGameResolution = callable<[appid: number, res: string], boolean>("set_game_resolution");
declare const SteamClient: any;
// роздільності 16:10 для екрана 1920×1200 (Legion Go S)
const RES_16_10 = [
  { w: 1920, h: 1200, label: "Full HD+ 1920×1200 (рідна)" },
  { w: 1440, h: 900, label: "900p 1440×900" },
  { w: 1280, h: 800, label: "800p 1280×800" },
];
const setGameEnabled = callable<[appid: number, enabled: boolean], boolean>("set_game_enabled");
const restoreOriginal = callable<[], boolean>("restore_original");

const ENGINE: Record<string, string> = {
  unreal: "Unreal Engine",
  unity: "Unity",
  unknown: "невідомий рушій",
};

const TUNE_STATE: Record<string, string> = {
  tuning: "підбирається",
  done: "підібрано",
  cpu: "не впливає на FPS",
  off: "вимкнено для гри",
  wait: "очікує",
  user: "твої налаштування",
};

const PHASE: Record<string, string> = {
  idle: "очікування",
  waiting: "чекає на кнопку",
  warmup: "розігрів",
  probe: "вимірювання",
  locked: "стабільно",
  manual: "вручну",
  disabled: "вимкнено",
};

function hhmm(t: number): string {
  const d = new Date(t * 1000);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function runningAppId(): number | null {
  const app = (Router as any).MainRunningApp;
  const id = Number(app?.appid);
  return Number.isSafeInteger(id) && id > 0 ? id : null;
}

type Game = { appid: number; name: string; enabled: boolean; seen: number; opti: boolean };

type OptiGame = {
  enabled: boolean;
  tag: string | null;
  dll: string | null;
  upscaler: string | null;
  error: string;
  target: string;
  engine: string;
  unsupported: string;
  anticheat: string;
  found: boolean;
  check: {
    t: number;
    status: string;
    title: string;
    message: string;
    fsr_version: string;
    proxy: string;
  } | null;
  dll_tried: string[];
  dll_ok: string;
  disabled_reason: string;
  ref: string;
  ref_tag: string;
  pd: PdStatus | null;
  method: string; // "" | "direct" | "optiscaler"
  planned: string; // що вибере кнопка: "direct" («один файл») | "optiscaler"
  native_fsr31: boolean;
  direct_variant: string;
  direct_confirmed: boolean;
  direct_tried: string[];
};
type PdStatus = { present: boolean; verified: boolean; name: string; url: string; error?: string };
type OptiStatus = {
  gpu: { vendor: string; arch: string; name: string; forced?: boolean };
  detected_arch: string;
  current: string;
  installed: string | null;
  latest: string | null;
  latest_name: string | null;
  published: string;
  update: boolean;
  check_error: string;
  checked_at: number;
  extractor: string;
  upscaler_auto: string;
  game: OptiGame | null;
  helixsr: { tag: string; path: string } | null;
};
type OptiResult = {
  ok: boolean;
  error?: string;
  upscaler?: string;
  dll?: string;
  tag?: string;
  ref?: string;
  need_pd?: boolean;
  method?: string;
};

const startTuning = callable<[], boolean>("start_tuning");
const optiStatus = callable<[appid?: number], OptiStatus>("opti_status");
const optiUpdate = callable<[], OptiResult>("opti_update");
const optiEnable = callable<[appid: number], OptiResult>("opti_enable");
const optiDisable = callable<[appid: number], OptiResult>("opti_disable");
const optiDirectFeedback = callable<[appid: number, ok: boolean], OptiResult>("opti_direct_feedback");
const optiPdScan = callable<[], PdStatus>("opti_pd_scan");
const optiPdImport = callable<[path: string], PdStatus>("opti_pd_import");

// Лаунчер окремого плагіна Decky LSFG-VK (з бекенда): додається в параметри разом з AutoFPS
let lsfgLauncher = "";
let fgExternal = true;

// Номер запиту: відповідь для попередньо вибраної гри не перезапише нову
let readSeq = 0;
// Вибір гри переживає закриття/відкриття меню
let lastSelected: number | null = null;

// memo: меню оновлює статус щосекунди — цей розділ від цього не перемальовується,
// інакше випадний список Steam скидає вибір на першу гру
const GameLaunchSection = memo(function GameLaunchSection() {
  const [games, setGames] = useState<Game[]>([]);
  const [appid, setAppidState] = useState<number | null>(lastSelected ?? runningAppId());
  const setAppid = (id: number | null) => {
    lastSelected = id;
    setAppidState(id);
  };
  const [running] = useState<number | null>(runningAppId());
  const [options, setOptions] = useState<string | null>(null);
  const [readError, setReadError] = useState(false);
  const [busy, setBusy] = useState(false);
  const [opti, setOpti] = useState<OptiStatus | null>(null);
  const [optiBusy, setOptiBusy] = useState("");

  const reloadOpti = (id: number | null) => {
    if (id === null) return;
    optiStatus(id)
      .then((st) => {
        if (lastSelected === id || lastSelected === null) setOpti(st);
      })
      .catch(() => setOpti(null));
  };

  const reloadGames = () =>
    listGames()
      .then((list) => {
        setGames(list);
        return list;
      })
      .catch(() => [] as Game[]);

  useEffect(() => {
    reloadGames().then((list) => {
      if (lastSelected === null && runningAppId() === null && list.length) {
        setAppid(list[0].appid);
      }
    });
  }, []);

  const loadOptions = (id: number) => {
    const seq = ++readSeq;
    setOptions(null);
    setReadError(false);
    readLaunchOptions(id)
      .then((o) => {
        if (seq === readSeq) setOptions(o);
      })
      .catch(() => {
        if (seq === readSeq) {
          setReadError(true);
          setOptions("");
        }
      });
  };

  const [nativeH, setNativeH] = useState(0);
  const [res, setRes] = useState<string | null>(null);
  useEffect(() => {
    getStatus().then((s) => setNativeH(s.device.res?.[1] ?? 0)).catch(() => setNativeH(0));
  }, []);
  const loadRes = (id: number) => {
    setRes(null);
    try {
      Promise.resolve(SteamClient.Apps.GetResolutionOverrideForApp(id))
        .then((r: string) => { if (lastSelected === id || lastSelected === null) setRes(r || "Default"); })
        .catch(() => setRes("Default"));
    } catch {
      setRes("Default");
    }
  };

  useEffect(() => {
    if (appid !== null) {
      loadOptions(appid);
      setOpti(null);
      reloadOpti(appid);
      loadRes(appid);
    }
  }, [appid]);

  const game = games.find((g) => g.appid === appid);
  // стабільний масив опцій — змінюється лише коли змінився список ігор
  const opts = useMemo(() => {
    const list = games.map((g) => ({
      data: g.appid,
      label: `${g.appid === running ? "▶ " : ""}${g.enabled ? "✓ " : ""}${g.opti ? "⚡" : ""}${g.name}`,
    }));
    if (running !== null && !games.some((g) => g.appid === running)) {
      list.unshift({ data: running, label: `▶ Поточна гра (${running})` });
    }
    return list;
  }, [games, running]);
  // якщо список змінився (з'явилась ✓), список створюється наново з правильним вибором
  const optsKey = useMemo(() => opts.map((o) => o.label).join("|").length + ":" + opts.length, [opts]);
  const enabled = options !== null && hasWrapper(options);
  // AutoFPS увімкнено, але без лаунчера Decky LSFG-VK — пропонуємо оновити параметр
  const needsLsfg = options !== null && hasWrapper(options) && fgExternal && !!lsfgLauncher && !hasLsfg(options, lsfgLauncher);
  const outdated = options !== null && (isOldWrapper(options) || needsLsfg);
  // параметрами гри керує Decky Launch Options — не чіпаємо, щоб не зламати запуск
  const dlo = options !== null && /\.dlo\/run/.test(options);

  let statusLabel = "Читаю параметри запуску…";
  if (options !== null) {
    statusLabel = outdated
      ? needsLsfg
        ? "⚠ Без Lossless — натисни «Оновити», щоб додати лаунчер Decky LSFG-VK"
        : "⚠ Старий параметр запуску — натисни «Оновити»"
      : enabled
        ? "✓ AutoFPS увімкнено"
        : "AutoFPS не увімкнено";
    if (readError) statusLabel = "⚠ Steam не віддав параметри запуску";
    if (dlo) statusLabel = "⚠ Цією грою керує Decky Launch Options";
  }
  const seenText = game?.seen
    ? `Останній запуск з AutoFPS: ${new Date(game.seen * 1000).toLocaleString()}`
    : "З AutoFPS ще не запускалася";

  // Одна кнопка: підключити AutoFPS до гри (якщо ще ні) + завантажити й поставити OptiScaler
  const toggleOpti = async () => {
    if (appid === null || !opti?.game) return;
    const turnOn = !opti.game.enabled;
    try {
      if (turnOn) {
        setOptiBusy(opti.installed ? "Встановлюю в гру…" : "Завантажую OptiScaler…");
        const current = await readLaunchOptions(appid);
        if (!hasWrapper(current) || isOldWrapper(current)) {
          const ok = await writeLaunchOptions(appid, addWrapper(current, fgExternal ? lsfgLauncher : ""));
          if (!ok) throw new Error("Steam не зберіг параметри запуску");
          await setGameEnabled(appid, true);
        }
        const r = await optiEnable(appid);
        if (r.ok && r.method === "bridge") {
          toaster.toast({
            title: "AutoFPS · FSR 4 увімкнено",
            body: `${game?.name ?? appid}: міст FSR2→FSR4. У грі вибери FSR 2 (будь-який режим) — працюватиме FSR 4, інтерфейс чіткий`,
          });
        } else if (r.ok && r.method === "direct") {
          toaster.toast({
            title: "AutoFPS · FSR 4 увімкнено",
            body:
              `${game?.name ?? appid}: ${r.upscaler}. Запусти гру й вибери FSR у налаштуваннях графіки — ` +
              "у кутку з'явиться напис AMD «FSR 4»",
          });
        } else if (r.need_pd) {
          toaster.toast({ title: "AutoFPS · потрібен ще один файл", body: r.error ?? "" });
        } else if (!r.ok) {
          throw new Error(r.error);
        } else {
          toaster.toast({
            title: r.ref ? "AutoFPS · FSR увімкнено" : "AutoFPS · DLSS увімкнено",
            body: r.ref
              ? `${game?.name ?? appid}: ${r.upscaler} через REFramework. У грі відкрий меню REFramework ` +
                "(Insert або L3+R3) → TemporalUpscaler → увімкни"
              : `${game?.name ?? appid}: DLSS → ${r.upscaler}. Запусти гру й вибери DLSS у налаштуваннях графіки`,
          });
        }
      } else {
        setOptiBusy("Прибираю з гри…");
        const r = await optiDisable(appid);
        if (!r.ok) throw new Error(r.error);
        toaster.toast({ title: "AutoFPS", body: "FSR 4 / OptiScaler прибрано, оригінальні файли гри повернуто" });
      }
    } catch (e) {
      toaster.toast({ title: "AutoFPS · DLSS", body: `Не вдалося: ${e instanceof Error ? e.message : String(e)}` });
    }
    setOptiBusy("");
    await reloadGames();
    loadOptions(appid);
    reloadOpti(appid);
  };

  const og = opti?.game;
  let optiLabel = "DLSS (OptiScaler)";
  let optiDesc = "";
  let optiBlocked = false;
  if (og) {
    if (og.unsupported) {
      optiLabel = "DLSS: недоступно для цієї гри";
      optiDesc = og.unsupported;
      optiBlocked = !og.enabled;
    } else if (og.anticheat && !og.enabled) {
      optiLabel = "DLSS: вимкнено через античит";
      optiDesc = `У грі є ${og.anticheat}. Підміна бібліотек в онлайн-іграх може призвести до бану`;
      optiBlocked = true;
    } else if (!og.found) {
      optiLabel = "DLSS: теку гри не знайдено";
      optiBlocked = !og.enabled;
    } else if (og.enabled && og.method === "bridge") {
      optiLabel = "⚡ FSR 4 через міст AutoFPS (інтерфейс чіткий)";
      const chk = og.check
        ? `\nОстання перевірка: ${og.check.title}. ${og.check.message}`
        : "\nУ грі вибери FSR 2 (будь-який режим) — замість нього працюватиме FSR 4. Режим «Продуктивність» дає найбільше FPS";
      optiDesc = "Без REFramework: міст підміняє FSR 2 гри на FSR 4 до того, як гра малює інтерфейс" +
        chk + (og.error ? `\n⚠ ${og.error}` : "");
    } else if (og.planned === "bridge" && !og.enabled) {
      optiLabel = "FSR 4 вимкнено · доступний міст AutoFPS";
      optiDesc = "Для цієї гри є власний міст FSR2→FSR4: чіткий інтерфейс, без REFramework і Nexus. " +
        "Кнопка нижче поставить у грі FSR 2 «Продуктивність» (рендер 50%) — FSR 4 тримає картинку майже як «Якість», а FPS значно вищий";
    } else if (og.enabled && og.method === "direct") {
      optiLabel = `⚡ FSR 4 у вбудований FSR 3.1 гри (${og.upscaler ?? "один файл"})`;
      const chk = og.direct_confirmed
        ? "\n✓ Підтверджено: напис FSR 4 був"
        : og.check
          ? `\nОстання перевірка: ${og.check.title}. ${og.check.message}`
          : "\nУ грі вибери FSR у налаштуваннях графіки — у кутку з'явиться напис AMD «FSR 4»";
      optiDesc = "Без OptiScaler: поруч з FSR 3.1 гри лежить бібліотека драйвера FSR 4 (amdxcffx64.dll)" +
        chk + (og.error ? `\n⚠ ${og.error}` : "");
    } else if (og.enabled && og.ref) {
      optiLabel = `⚡ ${og.upscaler ?? "FSR"} через REFramework (OptiScaler ${og.tag ?? ""})`;
      const chk = og.check
        ? `\nОстання перевірка: ${og.check.title}. ${og.check.message}`
        : "\nУ грі: меню REFramework (Insert або L3+R3) → TemporalUpscaler → увімкни (один раз)";
      optiDesc = `REFramework ${og.ref_tag}${og.ref === "pd" ? " + " + (og.pd?.name ?? "PDPerfPlugin") : ""}, ` +
        `OptiScaler ${og.dll}. Вбудований FSR гри вимкнено — масштабує REFramework` + chk +
        (og.error ? `\n⚠ ${og.error}` : "");
    } else if (og.enabled) {
      optiLabel = `⚡ DLSS → ${og.upscaler ?? "FSR"} (OptiScaler ${og.tag ?? ""})`;
      const chk = og.check
        ? `\nОстання перевірка (${new Date(og.check.t * 1000).toLocaleString()}): ${og.check.title}. ${og.check.message}`
        : "\nПеревірка буде після першої гри (пограй хоча б хвилину)";
      optiDesc = `${og.engine}: ${og.target}, ${og.dll}. У грі вибери DLSS у налаштуваннях графіки` +
        chk + (og.error ? `\n⚠ ${og.error}` : "");
    } else if (og.disabled_reason) {
      optiLabel = "DLSS вимкнено самоперевіркою";
      optiDesc = `${og.disabled_reason}. Можна спробувати ще раз — перебір почнеться спочатку`;
    } else if (og.planned === "direct") {
      optiLabel = "FSR 4 вимкнено · у гри є свій FSR 3.1";
      optiDesc = `${og.engine}: досить одного файлу — AutoFPS покладе бібліотеку FSR 4 ` +
        "(amdxcffx64.dll) поруч з FSR 3.1 гри, і вбудований FSR стане FSR 4. OptiScaler не потрібен";
    } else if (og.ref === "pd" && og.pd && !og.pd.present) {
      optiLabel = `FSR 4 для цієї гри: бракує ${og.pd.name}`;
      optiDesc =
        "У цій грі немає вбудованого DLSS — FSR 4 працює через REFramework і плагін PureDark. " +
        "Усе інше AutoFPS завантажить сам, а цей файл лежить лише на Nexus Mods за входом:\n" +
        "1. Режим робочого столу → браузер → nexusmods.com/site/mods/502 (вкладка Files)\n" +
        `2. Завантаж ${og.pd.name} (саме 1.1.2 — 1.2.0 не працює)\n` +
        "3. Залиш файл у «Завантаженнях» — AutoFPS знайде його сам, або вибери кнопкою нижче";
      optiBlocked = true;
    } else {
      optiLabel = og.ref ? "FSR (REFramework + OptiScaler) вимкнено" : "DLSS вимкнено";
      optiDesc = `${og.engine} → буде ${opti?.upscaler_auto ?? "FSR 3.1"} замість DLSS. ` +
        (opti?.installed ? `OptiScaler ${opti.installed}` : "OptiScaler завантажиться автоматично");
    }
  }

  const toggle = async () => {
    if (appid === null) return;
    setBusy(true);
    try {
      // перечитуємо саме цю гру безпосередньо перед зміною
      const current = await readLaunchOptions(appid);
      // стара форма — оновлюємо на новий лаунчер, а не вимикаємо
      const turnOn = !hasWrapper(current) || isOldWrapper(current) ||
        (fgExternal && !!lsfgLauncher && !hasLsfg(current, lsfgLauncher));
      const next = turnOn ? addWrapper(current, fgExternal ? lsfgLauncher : "") : removeWrapper(current);
      const ok = await writeLaunchOptions(appid, next);
      if (ok) await setGameEnabled(appid, turnOn);
      await reloadGames();
      loadOptions(appid);
      toaster.toast({
        title: "AutoFPS",
        body: ok
          ? `${game?.name ?? appid}: ${turnOn ? "увімкнено, діє з наступного запуску" : "вимкнено"}`
          : "Steam не зберіг параметри запуску",
      });
    } catch (e) {
      toaster.toast({ title: "AutoFPS", body: `Не вдалося прочитати параметри гри: ${String(e)}` });
    }
    setBusy(false);
  };

  return (
    <PanelSection title="Підключення до гри">
      <PanelSectionRow>
        <DropdownItem
          key={optsKey}
          label="Гра"
          description="✓ — AutoFPS, ⚡ — DLSS (OptiScaler), ▶ — зараз запущена"
          rgOptions={opts}
          selectedOption={appid}
          strDefaultLabel={games.length ? "Виберіть гру" : "Завантаження…"}
          onChange={(o: { data: number }) => setAppid(Number(o.data))}
        />
      </PanelSectionRow>
      {appid !== null && (
        <PanelSectionRow>
          <Field
            label={statusLabel}
            description={`${options ? `Параметри: ${options}` : "Параметрів запуску немає"}\n${seenText}`}
          />
        </PanelSectionRow>
      )}
      {dlo && (
        <PanelSectionRow>
          <Field
            label="AutoFPS не змінюватиме параметри цієї гри"
            description="Два плагіни, що керують запуском однієї гри, ламають її старт. Вимкни Decky Launch Options для цієї гри, і кнопка з'явиться"
          />
        </PanelSectionRow>
      )}
      {appid !== null && nativeH >= 1080 && res !== null && (
        <PanelSectionRow>
          <DropdownItem
            label="Роздільна здатність гри"
            description="Гра рендерить у вибраній роздільності, екран масштабує сам. AutoFPS підбере графіку під неї (з наступного запуску)"
            rgOptions={RES_16_10.map((r) => ({ data: r.h >= nativeH ? "Default" : `${r.w}x${r.h}`, label: r.label }))}
            selectedOption={/^\d+x\d+$/.test(res) && RES_16_10.some((r) => `${r.w}x${r.h}` === res && r.h < nativeH) ? res : "Default"}
            onChange={async (o: { data: string }) => {
              try {
                SteamClient.Apps.SetAppResolutionOverride(appid, o.data);
                await setGameResolution(appid, o.data);
                setRes(o.data);
                toaster.toast({
                  title: "AutoFPS",
                  body: o.data === "Default"
                    ? "Гра працюватиме в рідній роздільності"
                    : `Роздільність гри: ${o.data.replace("x", "×")}. Графіка підбереться заново з наступного запуску`,
                });
              } catch (e) {
                toaster.toast({ title: "AutoFPS", body: `Steam не змінив роздільність: ${String(e)}` });
              }
            }}
          />
        </PanelSectionRow>
      )}
      {appid !== null && options !== null && !dlo && (
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={busy} onClick={toggle}>
            {outdated
              ? "Оновити параметр запуску"
              : enabled
                ? "Вимкнути AutoFPS у цій грі"
                : "Увімкнути AutoFPS у цій грі"}
          </ButtonItem>
        </PanelSectionRow>
      )}
      {appid !== null && og && !dlo && (
        <>
          <PanelSectionRow>
            <Field label={optiLabel} description={optiDesc} />
          </PanelSectionRow>
          {!optiBlocked && (
            <PanelSectionRow>
              <ButtonItem layout="below" disabled={!!optiBusy} onClick={toggleOpti}>
                {optiBusy ||
                  (og.enabled
                    ? og.ref || og.method === "direct" || og.method === "bridge" ? "Вимкнути FSR 4 у цій грі" : "Вимкнути DLSS у цій грі"
                    : og.ref || og.planned === "direct" || og.planned === "bridge" ? "⚡ Увімкнути FSR 4 у цій грі" : "⚡ Увімкнути DLSS у цій грі")}
              </ButtonItem>
            </PanelSectionRow>
          )}
          {og.enabled && og.method === "direct" && !og.direct_confirmed && (
            <>
              <PanelSectionRow>
                <ButtonItem
                  layout="below"
                  disabled={!!optiBusy}
                  onClick={async () => {
                    const r = await optiDirectFeedback(appid, true);
                    toaster.toast({ title: "AutoFPS", body: r.ok ? "Чудово! Напис більше не показуватиметься" : `Помилка: ${r.error}` });
                    reloadOpti(appid);
                  }}
                >
                  Напис «FSR 4» був ✓
                </ButtonItem>
              </PanelSectionRow>
              <PanelSectionRow>
                <ButtonItem
                  layout="below"
                  disabled={!!optiBusy}
                  onClick={async () => {
                    setOptiBusy("Пробую інший варіант…");
                    const r = await optiDirectFeedback(appid, false);
                    toaster.toast({
                      title: "AutoFPS · FSR 4",
                      body: !r.ok
                        ? `Не вдалося: ${r.error}`
                        : r.method === "direct"
                          ? `Наступний варіант: ${r.upscaler}. Запусти гру ще раз`
                          : `«Один файл» не спрацював — поставлено OptiScaler (${r.upscaler}). У грі вибери FSR або DLSS`,
                    });
                    setOptiBusy("");
                    reloadOpti(appid);
                  }}
                >
                  {optiBusy || "Напису не було — спробувати інший варіант"}
                </ButtonItem>
              </PanelSectionRow>
            </>
          )}
          {og.ref === "pd" && og.pd && !og.pd.present && !og.enabled && og.planned !== "direct" && og.planned !== "bridge" && (
            <>
              <PanelSectionRow>
                <ButtonItem
                  layout="below"
                  disabled={!!optiBusy}
                  onClick={async () => {
                    setOptiBusy("Шукаю у Завантаженнях…");
                    const r = await optiPdScan();
                    toaster.toast({
                      title: "AutoFPS",
                      body: r.present
                        ? `Знайдено ${r.name}${r.verified ? "" : " (не 1.1.2 — може не працювати)"}. Тепер увімкни FSR 4`
                        : "Файл не знайдено в Завантаженнях і на картках пам'яті",
                    });
                    setOptiBusy("");
                    reloadOpti(appid);
                  }}
                >
                  {optiBusy || "Перевірити Завантаження"}
                </ButtonItem>
              </PanelSectionRow>
              <PanelSectionRow>
                <ButtonItem
                  layout="below"
                  disabled={!!optiBusy}
                  onClick={async () => {
                    try {
                      const chosen: any = await openFilePicker(0 as never, "/home", true, true);
                      const path = chosen?.realpath || chosen?.path;
                      if (!path) return;
                      const r = await optiPdImport(path);
                      toaster.toast({
                        title: "AutoFPS",
                        body: r.present ? `${r.name} додано. Тепер увімкни FSR 4` : `Не вдалося: ${r.error}`,
                      });
                      reloadOpti(appid);
                    } catch {
                      /* вибір скасовано */
                    }
                  }}
                >
                  Вибрати файл (DLL або архів)…
                </ButtonItem>
              </PanelSectionRow>
            </>
          )}
        </>
      )}
    </PanelSection>
  );
});

function OptiSection({
  settings,
  update,
}: {
  settings: Settings;
  update: (key: keyof Settings, value: unknown) => Promise<void>;
}) {
  const [st, setSt] = useState<OptiStatus | null>(null);
  const [busy, setBusy] = useState("");
  useEffect(() => {
    optiStatus().then(setSt).catch(() => setSt(null));
  }, []);
  if (!st) return null;

  const upOptions = [
    { data: "auto", label: "Авто (FSR 4)" },
    { data: "fsr4", label: "FSR 4 — найкраща картинка" },
    { data: "fsr31", label: "FSR 3.1 — найшвидше" },
  ];
  let version = st.installed ? `OptiScaler ${st.installed}` : "OptiScaler ще не завантажено";
  if (st.latest && st.update && st.installed) version += ` · доступна ${st.latest}`;

  return (
    <PanelSection title="FSR 4 / DLSS">
      {st.update && st.installed && (
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={!!busy}
            onClick={async () => {
              setBusy("Оновлюю…");
              const r = await optiUpdate();
              toaster.toast({
                title: "AutoFPS",
                body: r.ok ? `OptiScaler ${r.tag}: ігри оновляться при наступному запуску` : `Не вдалося: ${r.error}`,
              });
              setSt(await optiStatus());
              setBusy("");
            }}
          >
            {busy || `⬆ Оновити OptiScaler до ${st.latest}`}
          </ButtonItem>
        </PanelSectionRow>
      )}
      <PanelSectionRow>
        <Field label={version} description={`Відеокарта: ${st.gpu.name}`} />
      </PanelSectionRow>
      <PanelSectionRow>
        <DropdownItem
          label="Масштабування в іграх з DLSS"
          rgOptions={upOptions}
          selectedOption={["auto", "fsr4", "fsr31"].includes(settings.opti_upscaler) ? settings.opti_upscaler : "auto"}
          onChange={async (o: { data: string }) => {
            await update("opti_upscaler", o.data);
            setSt(await optiStatus());
          }}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <DropdownItem
          label="Resident Evil 4"
          description={
            "FSR 4 — найкраща картинка, FSR 3.1 — на ~15% більше FPS (діє одразу)." +
            (st.helixsr
              ? " HLX FSR 4 — FSR 4 від HelixSR Manager, зібраний для RDNA2/3 (з наступного запуску гри)"
              : " Встанови HelixSR Manager і збери HLX FSR 4 — з'явиться ще один варіант")
          }
          rgOptions={[
            { data: "4", label: "FSR 4" },
            { data: "3", label: "FSR 3.1 (швидше)" },
            ...(st.helixsr ? [{ data: "hlx", label: `HLX FSR 4 · HelixSR ${st.helixsr.tag}` }] : []),
          ]}
          selectedOption={settings.bridge_version ?? "4"}
          onChange={(o: { data: string }) => update("bridge_version", o.data)}
        />
      </PanelSectionRow>
    </PanelSection>
  );
}

function Content() {
  const [st, setSt] = useState<Status | null>(null);
  const [tab, setTabState] = useState(lastTab);
  const setTab = (t: string) => {
    lastTab = t;
    setTabState(t);
  };

  useEffect(() => {
    let alive = true;
    const poll = async () => {
      try {
        const s = await getStatus();
        lsfgLauncher = s.lsfg_launcher || "";
        fgExternal = s.settings.fg_mode === "external";
        if (alive) setSt(s);
      } catch {
        /* бекенд ще стартує */
      }
    };
    poll();
    const id = setInterval(poll, 1000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  if (!st) {
    return (
      <PanelSection>
        <PanelSectionRow>
          <Field label="Завантаження…" />
        </PanelSectionRow>
      </PanelSection>
    );
  }

  const s = st.settings;
  const update = async (key: keyof Settings, value: unknown) => {
    await setSetting(key, value);
    setSt({ ...st, settings: { ...s, [key]: value } as Settings });
  };

  const modeOptions = [
    ...(st.device.vrr ? [{ data: "vrr", label: `VRR до ${st.device.max_hz} (без ліміту)` }] : []),
    { data: "auto", label: "Авто (найвищий, що гра тримає)" },
    ...st.device.tiers.map((t) => ({ data: String(t), label: `${t} FPS` })),
  ];
  const modeLabel = (m: string) => modeOptions.find((o) => o.data === m)?.label ?? m;
  const qualityOptions = [
    { data: "auto", label: `Авто — з ліміту від батареї (${st.graphics_target ?? st.device.quality_target})` },
    ...Array.from(new Set([30, 40, 45, ...st.device.tiers]))
      .filter((t) => t <= Math.min(90, st.device.max_hz))
      .sort((a, b) => a - b)
      .map((t) => ({ data: String(t), label: `${t} FPS` })),
  ];
  const fgOptions = [
    { data: "external", label: "Окремий плагін Decky LSFG-VK" },
    { data: "off", label: "Вимкнено" },
    { data: "auto", label: "Авто (коли не тягне)" },
    { data: "always", label: "Завжди, якщо можна" },
  ];

  return (
    <>
      <PanelSection>
        <TabBar tab={tab} setTab={setTab} />
      </PanelSection>
      {tab === "game" && (
        <>
      <PanelSection title="Стан">
        <PanelSectionRow>
          <Field label="Пристрій" description={st.device.label} />
        </PanelSectionRow>
        <PanelSectionRow>
          <Field
            label={st.appid ? `Гра ${st.appid} · ${PHASE[st.phase] ?? st.phase}` : "Гра"}
            description={st.message}
          />
        </PanelSectionRow>
        {st.appid && (
          <PanelSectionRow>
            <Field
              label="FPS"
              description={
                `зараз ${st.fps_now ?? "—"} · ціль ${st.out || "—"}` +
                (st.mult > 1 ? ` (база ${st.base} × FG ${st.mult})` : "") +
                (st.native_p10 ? ` · p10 без ліміту ${st.native_p10}` : "")
              }
            />
          </PanelSectionRow>
        )}
        {st.appid && st.game_update && st.game_update.appid === st.appid && (
          <PanelSectionRow>
            <Field
              label="Гру оновлено"
              description={`Збірка ${st.game_update.old} → ${st.game_update.new}. FSR 4 і твіки перевірено, графіка перевіряється заново`}
            />
          </PanelSectionRow>
        )}
        {st.appid && st.phase === "waiting" && (
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              onClick={async () => {
                await startTuning();
                toaster.toast({ title: "AutoFPS", body: "Підбір почався — грай як звичайно ~30 с" });
              }}
            >
              ▶ Почати підбір
            </ButtonItem>
          </PanelSectionRow>
        )}
        {st.appid && st.phase !== "idle" && st.phase !== "waiting" && (
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={() => reprobe()}>
              Переміряти цю гру
            </ButtonItem>
          </PanelSectionRow>
        )}
      </PanelSection>

      <GameLaunchSection />
      <PanelSection title="Що зробив AutoFPS">
        {st.events.length === 0 ? (
          <PanelSectionRow>
            <Field
              label="Ще нічого"
              description="Запусти гру, підключену до AutoFPS, — тут з'являться всі зміни"
            />
          </PanelSectionRow>
        ) : (
          st.events.slice(0, 6).map((e, i) => (
            <PanelSectionRow key={`${e.t}-${i}`}>
              <Field label={`${hhmm(e.t)} · ${e.title.replace(/^AutoFPS:?\s*/, "")}`} description={e.body} />
            </PanelSectionRow>
          ))
        )}
      </PanelSection>

        </>
      )}
      {tab === "fps" && (
        <>
      <PanelSection title="Ліміт FPS на екрані">
        <PanelSectionRow>
          <Field
            label={`Зараз ${st.power?.ac ? "🔌 від зарядки" : "🔋 від батареї"}: ${modeLabel(st.power?.mode ?? s.mode)}`}
            description={
              "Скільки кадрів буде на екрані. З Lossless — разом зі згенерованими. Перемикається сам, коли під'єднуєш зарядку" +
              (st.device.vrr
                ? `\nVRR екрана: ${st.device.vrr_range ? `${st.device.vrr_range[0]}–${st.device.vrr_range[1]} Гц` : "діапазон невідомий"}` +
                  (st.device.vrr_range
                    ? st.device.vrr_range[1] >= 2 * st.device.vrr_range[0]
                      ? ` — нижче ${st.device.vrr_range[0]} FPS gamescope подвоює кадри (LFC), плавно`
                      : ` — нижче ${st.device.vrr_range[0]} FPS VRR не працює`
                    : "")
                : "")
            }
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <DropdownItem
            label="🔋 Від батареї"
            rgOptions={modeOptions}
            selectedOption={!s.bat_mode || s.bat_mode === "same" ? s.mode : s.bat_mode}
            onChange={(o: { data: string }) => update("bat_mode", o.data)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <DropdownItem
            label="🔌 Від зарядки"
            rgOptions={modeOptions}
            selectedOption={!s.ac_mode || s.ac_mode === "same" ? s.mode : s.ac_mode}
            onChange={(o: { data: string }) => update("ac_mode", o.data)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <Field
            label={`Графіка підбирається під ${st.graphics_target ?? "—"} FPS гри`}
            description={
              "Справжніх кадрів, без генерації — щоб гра рівно тримала ліміт від батареї" +
              (s.quality_target !== "auto" ? " (задано вручну на вкладці «Ще»)" : "")
            }
          />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Живлення">
        <PanelSectionRow>
          <Field
            label={
              st.power?.tdp_available
                ? `${st.power.level ?? "як у Steam"}${st.power.level_i != null ? ` (${st.power.level_i + 1}/${st.power.levels})` : ""}`
                : "Керування живленням недоступне"
            }
            description={
              (st.power?.tdp_available
                ? TDP_STATE[st.power.tdp_state] ?? st.power.tdp_state
                : st.power?.tdp_error || "немає steamos-manager") +
              (st.power?.battery != null ? ` · батарея ${st.power.battery}%` : "") +
              (st.power?.telemetry?.temp != null
                ? ` · ${st.power.hot ? "🔥 " : ""}APU ${st.power.telemetry.temp} °C` +
                  (st.power.telemetry.watts ? ` · ${st.power.telemetry.watts} Вт` : "")
                : "")
            }
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <DropdownItem
            label="Тихо й економно"
            description={
              st.power?.kind === "profiles"
                ? "Найтихіший профіль Lenovo, на якому гра тримає ліміт FPS (без гучного «Custom»)"
                : "Найменший TDP, на якому гра тримає ліміт FPS"
            }
            disabled={!st.power?.tdp_available}
            rgOptions={[
              { data: "always", label: "Завжди" },
              { data: "battery", label: "Лише від батареї" },
              { data: "off", label: "Вимкнено" },
            ]}
            selectedOption={s.auto_tdp ?? "always"}
            onChange={(o: { data: string }) => update("auto_tdp", o.data)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Частота GPU"
            description={
              !st.power?.gpu?.available
                ? st.power?.gpu?.error || "Недоступно"
                : (st.power.gpu.clock ? `${st.power.gpu.clock} МГц` : "Авто") +
                  (({ ref: " · еталонний замір", descending: " · підбираю", settled: " · підібрано" } as Record<string, string>)[
                    st.power.gpu.state
                  ] ?? "") +
                  " — найнижча фіксована частота, з якою FPS і кадри рівні (як «Ручна частота GPU» у Steam)"
            }
            disabled={!st.power?.gpu?.available}
            checked={s.auto_gpu_clock ?? true}
            onChange={(v: boolean) => update("auto_gpu_clock", v)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Ліміт FPS через MangoHud"
            description={
              (st.mango_cap ? `Діє: ${st.mango_cap} FPS гри · ` : "") +
              "Коли ліміт Steam не діє (напр. з Decky LSFG-VK), FPS обмежує MangoHud — меню й легкі сцени не палять батарею"
            }
            checked={s.mango_limiter ?? true}
            onChange={(v: boolean) => update("mango_limiter", v)}
          />
        </PanelSectionRow>
      </PanelSection>
        </>
      )}
      {tab === "gfx" && (
        <>
      <PanelSection title="Графіка гри (автопідбір)">
        <PanelSectionRow>
          <ToggleField
            label="Автоналаштування графіки"
            description="Підбирає якість за кілька запусків гри"
            checked={s.autotune}
            onChange={(v: boolean) => update("autotune", v)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Твік Engine.ini для Unreal Engine 5"
            description="Менше підвисань (кеш шейдерів), рівніший кадр, легші Nanite/Lumen/туман. Діє з наступного запуску гри"
            checked={s.ue5_tweak !== false}
            onChange={(v: boolean) => update("ue5_tweak", v)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Твік boot.config для Unity"
            description="Багатопотокова підготовка кадру (graphics jobs). Якщо гра почне падати на старті — AutoFPS сам прибере твік"
            checked={s.unity_tweak !== false}
            onChange={(v: boolean) => update("unity_tweak", v)}
          />
        </PanelSectionRow>
        {st.tune && (
          <PanelSectionRow>
            <Field
              label={
                `${ENGINE[st.tune.engine ?? ""] ?? "Гра"} · ${TUNE_STATE[st.tune.state]}` +
                (st.tune.steps && st.tune.level !== null
                  ? ` · сходинка ${st.tune.level + 1}/${st.tune.steps}`
                  : "")
              }
              description={[st.tune.summary, st.tune.note].filter(Boolean).join(" — ")}
            />
          </PanelSectionRow>
        )}
        {st.tune && st.tune.engine && st.tune.engine !== "unknown" && (
          <>
            <PanelSectionRow>
              <ButtonItem
                layout="below"
                onClick={async () => {
                  const r = await retune(st.tune?.appid ?? undefined);
                  toaster.toast({ title: r.ok ? "AutoFPS · графіку буде підібрано заново" : "AutoFPS", body: r.message });
                }}
              >
                Підібрати графіку заново
              </ButtonItem>
            </PanelSectionRow>
            <PanelSectionRow>
              <ButtonItem
                layout="below"
                onClick={async () => {
                  const ok = await restoreOriginal();
                  toaster.toast({
                    title: "AutoFPS",
                    body: ok ? "Оригінальні налаштування гри повернуться при наступному запуску" : "Цю гру ще не запускали з AutoFPS",
                  });
                }}
              >
                Повернути оригінальні налаштування
              </ButtonItem>
            </PanelSectionRow>
          </>
        )}
      </PanelSection>

      <OptiSection settings={s} update={update} />
        </>
      )}
      {tab === "fg" && (
        <>
      <PanelSection title="Фрейм-генерація">
        <PanelSectionRow>
          <DropdownItem
            label="Хто керує генерацією кадрів"
            rgOptions={fgOptions}
            selectedOption={s.fg_mode}
            onChange={(o: { data: string }) => update("fg_mode", o.data)}
          />
        </PanelSectionRow>
        {s.fg_mode === "external" ? (
          <>
            <PanelSectionRow>
              <DropdownItem
                label="Чиї параметри FG додавати"
                description="Лаунчер окремого плагіна додається в параметри запуску разом з AutoFPS"
                rgOptions={[
                  { data: "lsfg", label: `Lossless (Decky LSFG-VK)${st.fg_launchers?.lsfg ? "" : " — не встановлено"}` },
                  { data: "mako", label: `MAKO${st.fg_launchers?.mako ? "" : " — не встановлено"}` },
                  { data: "none", label: "Не додавати" },
                ]}
                selectedOption={s.fg_tool || "lsfg"}
                onChange={(o: { data: string }) => update("fg_tool", o.data)}
              />
            </PanelSectionRow>
            {(s.fg_tool || "lsfg") !== "none" && (
              <PanelSectionRow>
                <Field
                  label={st.lsfg_launcher ? `✓ Лаунчер ${st.lsfg_launcher}` :
                    (s.fg_tool === "mako" ? "⚠ MAKO не знайдено" : "⚠ Decky LSFG-VK не знайдено")}
                  description={
                    st.lsfg_launcher
                      ? "Кнопка «Увімкнути AutoFPS у цій грі» додає і цей лаунчер (інший — прибирає). Множник і налаштування — в його плагіні; AutoFPS лише враховує їх при підборі FPS"
                      : s.fg_tool === "mako"
                        ? "Встанови плагін MAKO Decky і натисни в ньому «Install MAKO Renderer» (з'явиться ~/.local/bin/mako-run)"
                        : "Встанови плагін Decky LSFG-VK і ввімкни його — тоді AutoFPS додаватиме його лаунчер разом зі своїм"
                  }
                />
              </PanelSectionRow>
            )}
          </>
        ) : (
          <>
            <PanelSectionRow>
              <Field label={st.lsfg.ready ? "✓ Готово" : "⚠ Не готово"} description={st.lsfg.hint} />
            </PanelSectionRow>
            <PanelSectionRow>
              <SliderField
                label="Мінімальний базовий FPS"
                description="Нижче за це FG не вмикається — інакше затримка вводу"
                value={s.fg_min_base}
                min={30}
                max={60}
                step={5}
                showValue
                onChange={(v: number) => update("fg_min_base", v)}
              />
            </PanelSectionRow>
            <PanelSectionRow>
              <ToggleField
                label="Дозволити ×3"
                description="Напр. 40 → 120 на Legion Go S. Більше артефактів."
                checked={s.fg_allow_3x}
                onChange={(v: boolean) => update("fg_allow_3x", v)}
              />
            </PanelSectionRow>
          </>
        )}
      </PanelSection>

        </>
      )}
      {tab === "more" && (
        <>
      <PanelSection title="Додатково">
        <PanelSectionRow>
          <ToggleField
            label="AutoFPS керує FPS"
            description="Вимкни, щоб AutoFPS нічого не обмежував у грі"
            checked={s.enabled}
            onChange={(v: boolean) => update("enabled", v)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Режим «Авто»: починати за кнопкою"
            description="Щоб не міряти FPS у меню гри: у режимі «Авто» замір FPS стартує після «▶ Почати підбір» на вкладці «Гра». Графіка й фіксований ліміт кнопки не потребують"
            checked={s.start_on_button ?? true}
            onChange={(v: boolean) => update("start_on_button", v)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <DropdownItem
            label="Під який FPS гри підбирати графіку"
            rgOptions={qualityOptions}
            selectedOption={s.quality_target}
            onChange={(o: { data: string }) => update("quality_target", o.data)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Залишати мої ручні налаштування"
            description="Увімкни, якщо після змін у меню гри AutoFPS має більше не чіпати її графіку"
            checked={s.respect_manual_graphics}
            onChange={(v: boolean) => update("respect_manual_graphics", v)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Знижувати роздільність заради графіки"
            description="Якщо так графіка буде помітно кращою — рендер ≈HD замість повної роздільності"
            checked={s.lowres_for_quality}
            onChange={(v: boolean) => update("lowres_for_quality", v)}
          />
        </PanelSectionRow>
      </PanelSection>
      <PanelSection title="Профілі ігор">
        <PanelSectionRow>
          <ToggleField
            label="Готові профілі з бази AutoFPS"
            description="Нова гра, для якої в базі є профіль під цей пристрій, одразу запускається з підібраною графікою, FPS і TDP — без кількох запусків підбору"
            checked={s.community_profiles !== false}
            onChange={(v: boolean) => update("community_profiles", v)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={!st.appid}
            onClick={async () => {
              const r = await shareProfile(st.appid ?? undefined);
              if (!r.ok || !r.url) {
                toaster.toast({ title: "AutoFPS", body: r.error ?? "Не вдалося" });
                return;
              }
              toaster.toast({
                title: "AutoFPS · профіль",
                body: "Відкриваю GitHub з готовим файлом — натисни «Commit changes» (потрібен вхід у GitHub)",
              });
              try {
                Navigation.NavigateToExternalWeb(r.url);
              } catch {
                SteamClient.URL.ExecuteSteamURL(`steam://openurl/${r.url}`);
              }
            }}
          >
            Поділитися профілем поточної гри
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
      <PanelSection title="Діагностика">
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={async () => {
              const r = await collectDiagnostics();
              toaster.toast({
                title: "AutoFPS",
                body: r.ok
                  ? `Збережено ${r.path}. Надішли цей файл у чат з Claude`
                  : `Не вдалося: ${r.error}`,
              });
            }}
          >
            Зібрати діагностику
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <Field
            label="Що далі"
            description="Файл ~/autofps-diagnostics.txt — зібрати одразу після гри й надіслати в чат"
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => forgetAll()}>
            Забути всі збережені ігри
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

        </>
      )}
    </>
  );
}

export default definePlugin(() => {
  // Сповіщення від бекенда показуємо навіть коли меню плагіна закрите
  const onToast = (title: string, body: string) => toaster.toast({ title, body });
  addEventListener<[string, string]>("autofps_toast", onToast);

  // Гра запущена, але не через AutoFPS — підказуємо один раз
  const warned = new Set<number>();
  let seenSince: Record<number, number> = {};
  const watch = window.setInterval(async () => {
    const running = runningAppId();
    if (running === null) {
      seenSince = {};
      return;
    }
    seenSince[running] ??= Date.now();
    if (warned.has(running) || Date.now() - seenSince[running] < 20000) return;
    try {
      const status = await getStatus();
      if (status.appid === null) {
        warned.add(running);
        toaster.toast({
          title: "AutoFPS не працює в цій грі",
          body: "Гру запущено без AutoFPS. Відкрий меню плагіна → Підключення до гри → Увімкнути, і перезапусти гру",
        });
      }
    } catch {
      /* бекенд недоступний */
    }
  }, 5000);

  return {
  name: "AutoFPS",
  titleView: <div className={staticClasses.Title}>AutoFPS</div>,
  content: <Content />,
  icon: <FaTachometerAlt />,
  onDismount() {
    removeEventListener("autofps_toast", onToast);
    window.clearInterval(watch);
  },
  };
});
