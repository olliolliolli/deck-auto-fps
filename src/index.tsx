import {
  ButtonItem,
  DropdownItem,
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
  WRAPPER,
  addWrapper,
  copyText,
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
  fg_mode: "off" | "auto" | "always";
  fg_min_base: number;
  fg_allow_3x: boolean;
  fg_flow_scale: number;
  fg_performance_mode: boolean;
  fps_counts_generated: boolean;
  autotune: boolean;
  quality_target: string;
  respect_manual_graphics: boolean;
  opti_upscaler: string;
  opti_gpu_arch: string;
  opti_fg: boolean;
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
  device: {
    id: string;
    label: string;
    tiers: number[];
    max_hz: number;
    quality_target: number;
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
};

const getStatus = callable<[], Status>("get_status");
const setSetting = callable<[key: string, value: unknown], boolean>("set_setting");
const reprobe = callable<[], boolean>("reprobe");
const forgetAll = callable<[], boolean>("forget_all");
const retune = callable<[], boolean>("retune");
const listGames = callable<[], Game[]>("list_games");
const collectDiagnostics = callable<
  [],
  { ok: boolean; path?: string; size?: number; error?: string }
>("collect_diagnostics");
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
};
type OptiResult = {
  ok: boolean;
  error?: string;
  upscaler?: string;
  dll?: string;
  tag?: string;
  ref?: string;
  need_pd?: boolean;
};

const optiStatus = callable<[appid?: number], OptiStatus>("opti_status");
const optiCheck = callable<[], OptiStatus>("opti_check");
const optiUpdate = callable<[], OptiResult>("opti_update");
const optiEnable = callable<[appid: number], OptiResult>("opti_enable");
const optiDisable = callable<[appid: number], OptiResult>("opti_disable");
const optiPdScan = callable<[], PdStatus>("opti_pd_scan");
const optiPdImport = callable<[path: string], PdStatus>("opti_pd_import");

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

  useEffect(() => {
    if (appid !== null) {
      loadOptions(appid);
      setOpti(null);
      reloadOpti(appid);
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
  const outdated = options !== null && isOldWrapper(options);
  // параметрами гри керує Decky Launch Options — не чіпаємо, щоб не зламати запуск
  const dlo = options !== null && /\.dlo\/run/.test(options);

  let statusLabel = "Читаю параметри запуску…";
  if (options !== null) {
    statusLabel = outdated
      ? "⚠ Старий параметр запуску — натисни «Оновити»"
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
          const ok = await writeLaunchOptions(appid, addWrapper(current));
          if (!ok) throw new Error("Steam не зберіг параметри запуску");
          await setGameEnabled(appid, true);
        }
        const r = await optiEnable(appid);
        if (r.need_pd) {
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
        toaster.toast({ title: "AutoFPS", body: "OptiScaler прибрано, оригінальні файли гри повернуто" });
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
      const turnOn = !hasWrapper(current) || isOldWrapper(current);
      const next = turnOn ? addWrapper(current) : removeWrapper(current);
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
                    ? og.ref ? "Вимкнути FSR у цій грі" : "Вимкнути DLSS у цій грі"
                    : og.ref ? "⚡ Увімкнути FSR 4 у цій грі" : "⚡ Увімкнути DLSS у цій грі")}
              </ButtonItem>
            </PanelSectionRow>
          )}
          {og.ref === "pd" && og.pd && !og.pd.present && !og.enabled && (
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
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          onClick={() => {
            const ok = copyText(`${WRAPPER} %command%`);
            toaster.toast({
              title: "AutoFPS",
              body: ok
                ? "Параметр запуску скопійовано"
                : "Steam не дав скопіювати — скористайтеся кнопкою «Увімкнути AutoFPS у цій грі»",
            });
          }}
        >
          Скопіювати параметр запуску
        </ButtonItem>
      </PanelSectionRow>
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
    { data: "auto", label: "Авто (FSR 4.1)" },
    { data: "fsr4", label: "FSR 4.1 — найкраща картинка" },
    { data: "fsr40", label: "FSR 4.0 — сумісний режим" },
    { data: "fsr31", label: "FSR 3.1 — найшвидше" },
    { data: "xess", label: "XeSS" },
  ];
  const archLabel: Record<string, string> = {
    rdna2: "RDNA2", rdna3: "RDNA3", "rdna3.5": "RDNA3.5", rdna4: "RDNA4", unknown: "невідомо",
  };
  const archOptions = [
    { data: "auto", label: `Авто (${archLabel[st.detected_arch] ?? st.detected_arch})` },
    { data: "rdna2", label: "RDNA2 (Z2 Go, Steam Deck)" },
    { data: "rdna3", label: "RDNA3 (Z1 Extreme)" },
    { data: "rdna4", label: "RDNA4" },
  ];
  const usingFsr4 = settings.opti_upscaler === "auto" || settings.opti_upscaler === "fsr4" ||
    settings.opti_upscaler === "fsr40";
  const fsr4slow = usingFsr4 && st.gpu.arch !== "rdna4";
  let version = st.installed ? `Встановлено ${st.installed}` : "Ще не завантажено";
  if (st.latest && st.update && st.installed) version += ` · доступна ${st.latest}`;
  if (st.latest && !st.update && st.installed) version += " · остання версія";
  if (st.check_error) version += ` · не вдалося перевірити оновлення`;

  return (
    <PanelSection title="DLSS (OptiScaler)">
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
        <Field
          label={version}
          description={
            `Відеокарта: ${st.gpu.name} (${archLabel[st.gpu.arch] ?? st.gpu.arch}` +
            `${st.gpu.forced ? ", вибрано вручну" : ""}) → ${st.current}. ` +
            `Справжній DLSS є лише на NVIDIA — OptiScaler підставляє ` +
            `замість нього FSR/XeSS, а гра думає, що це DLSS.` +
            (!st.extractor ? "\n⚠ Не знайдено 7z/bsdtar для розпакування" : "")
          }
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <DropdownItem
          label="Чим замінювати DLSS"
          rgOptions={upOptions}
          selectedOption={settings.opti_upscaler}
          onChange={async (o: { data: string }) => {
            await update("opti_upscaler", o.data);
            setSt(await optiStatus());
          }}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <DropdownItem
          label="Архітектура відеокарти"
          description="Від неї залежать файли FSR 4.1. Змінюй, лише якщо автовизначення помилилось"
          rgOptions={archOptions}
          selectedOption={settings.opti_gpu_arch}
          onChange={async (o: { data: string }) => {
            await update("opti_gpu_arch", o.data);
            setSt(await optiStatus());
          }}
        />
      </PanelSectionRow>
      {fsr4slow && (
        <PanelSectionRow>
          <Field
            label="FSR 4 на цій відеокарті важчий за FSR 3.1"
            description="Картинка помітно краща, але частина приросту FPS іде на сам FSR 4. Якщо гра не тягне — вибери FSR 3.1"
          />
        </PanelSectionRow>
      )}
      <PanelSectionRow>
        <ToggleField
          label="Генерація кадрів OptiScaler"
          description="Замість lsfg-vk у іграх з DLSS. Разом вони не працюють — AutoFPS вимкне lsfg-vk"
          checked={settings.opti_fg}
          onChange={(v: boolean) => update("opti_fg", v)}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          disabled={!!busy}
          onClick={async () => {
            setBusy("Перевіряю…");
            setSt(await optiCheck());
            setBusy("");
          }}
        >
          {busy === "Перевіряю…" ? busy : "Перевірити оновлення"}
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
}

function Content() {
  const [st, setSt] = useState<Status | null>(null);

  useEffect(() => {
    let alive = true;
    const poll = async () => {
      try {
        const s = await getStatus();
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
    { data: "auto", label: "Авто" },
    ...st.device.tiers.map((t) => ({ data: String(t), label: `${t} FPS` })),
  ];
  const qualityOptions = [
    { data: "auto", label: `Авто (${st.device.quality_target} FPS)` },
    ...[30, 40, 45, 60]
      .filter((t) => t <= st.device.max_hz)
      .map((t) => ({ data: String(t), label: `${t} FPS` })),
  ];
  const fgOptions = [
    { data: "off", label: "Вимкнено" },
    { data: "auto", label: "Авто (коли не тягне)" },
    { data: "always", label: "Завжди, якщо можна" },
  ];

  return (
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
        {st.appid && st.phase !== "idle" && (
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={() => reprobe()}>
              Переміряти цю гру
            </ButtonItem>
          </PanelSectionRow>
        )}
      </PanelSection>

      <GameLaunchSection />

      <PanelSection title="Режим">
        <PanelSectionRow>
          <ToggleField
            label="Автоматичний підбір"
            checked={s.enabled}
            onChange={(v: boolean) => update("enabled", v)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <DropdownItem
            label="Рівень FPS"
            rgOptions={modeOptions}
            selectedOption={s.mode}
            onChange={(o: { data: string }) => update("mode", o.data)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <SliderField
            label="Запас стабільності"
            description="Наскільки близько до цілі має бути 10% найгірших кадрів"
            value={Math.round(s.headroom * 100)}
            min={80}
            max={100}
            step={1}
            showValue
            onChange={(v: number) => update("headroom", v / 100)}
          />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Графіка гри (автопідбір)">
        <PanelSectionRow>
          <ToggleField
            label="Автоналаштування графіки"
            description="Unreal і Unity: підбирає якість за кілька запусків гри"
            checked={s.autotune}
            onChange={(v: boolean) => update("autotune", v)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <DropdownItem
            label="Під який FPS підбирати якість"
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
              <ButtonItem layout="below" onClick={() => retune()}>
                Підібрати графіку заново
              </ButtonItem>
            </PanelSectionRow>
            <PanelSectionRow>
              <ButtonItem layout="below" onClick={() => restoreOriginal()}>
                Повернути оригінальні налаштування
              </ButtonItem>
            </PanelSectionRow>
          </>
        )}
      </PanelSection>

      <OptiSection settings={s} update={update} />

      <PanelSection title="Фрейм-генерація (lsfg-vk)">
        <PanelSectionRow>
          <Field
            label={st.lsfg.ready ? "✓ Готово" : "⚠ Не готово"}
            description={st.lsfg.hint}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <DropdownItem
            label="Режим"
            rgOptions={fgOptions}
            selectedOption={s.fg_mode}
            onChange={(o: { data: string }) => update("fg_mode", o.data)}
          />
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
        <PanelSectionRow>
          <SliderField
            label="Flow scale"
            description="Нижче — швидше, вище — якісніше"
            value={Math.round(s.fg_flow_scale * 100)}
            min={25}
            max={100}
            step={5}
            showValue
            onChange={(v: number) => update("fg_flow_scale", v / 100)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Performance mode"
            checked={s.fg_performance_mode}
            onChange={(v: boolean) => update("fg_performance_mode", v)}
          />
        </PanelSectionRow>
      </PanelSection>

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
            label="Як надіслати"
            description="Файл ~/autofps-diagnostics.txt (тека «Домашня»). Найкраще зібрати під час або одразу після гри. Режим робочого столу → браузер → claude.ai → прикріпити файл до чату."
          />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Інше">
        <PanelSectionRow>
          <Field
            label="Порада"
            description="У меню швидкого доступу Steam (…) → Продуктивність вимкніть власний ліміт кадрів, щоб він не сперечався з AutoFPS."
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => forgetAll()}>
            Забути всі збережені ігри
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
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
