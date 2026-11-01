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

type Game = { appid: number; name: string; enabled: boolean; seen: number };

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
    if (appid !== null) loadOptions(appid);
  }, [appid]);

  const game = games.find((g) => g.appid === appid);
  // стабільний масив опцій — змінюється лише коли змінився список ігор
  const opts = useMemo(() => {
    const list = games.map((g) => ({
      data: g.appid,
      label: `${g.appid === running ? "▶ " : ""}${g.enabled ? "✓ " : ""}${g.name}`,
    }));
    if (running !== null && !games.some((g) => g.appid === running)) {
      list.unshift({ data: running, label: `▶ Поточна гра (${running})` });
    }
    return list;
  }, [games, running]);
  // якщо список змінився (з'явилась ✓), список створюється наново з правильним вибором
  const optsKey = useMemo(() => opts.map((o) => o.label).join("|").length + ":" + opts.length, [opts]);
  const enabled = options !== null && hasWrapper(options);

  let statusLabel = "Читаю параметри запуску…";
  if (options !== null) {
    statusLabel = enabled ? "✓ AutoFPS увімкнено" : "AutoFPS не увімкнено";
    if (readError) statusLabel = "⚠ Steam не віддав параметри запуску";
  }
  const seenText = game?.seen
    ? `Останній запуск з AutoFPS: ${new Date(game.seen * 1000).toLocaleString()}`
    : "З AutoFPS ще не запускалася";

  const toggle = async () => {
    if (appid === null) return;
    setBusy(true);
    try {
      // перечитуємо саме цю гру безпосередньо перед зміною
      const current = await readLaunchOptions(appid);
      const turnOn = !hasWrapper(current);
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
          description="✓ — AutoFPS увімкнено, ▶ — зараз запущена"
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
      {appid !== null && options !== null && (
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={busy} onClick={toggle}>
            {enabled ? "Вимкнути AutoFPS у цій грі" : "Увімкнути AutoFPS у цій грі"}
          </ButtonItem>
        </PanelSectionRow>
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
