import {
  ButtonItem,
  DropdownItem,
  Field,
  PanelSection,
  PanelSectionRow,
  SliderField,
  ToggleField,
  staticClasses,
} from "@decky/ui";
import { callable, definePlugin, toaster } from "@decky/api";
import { useEffect, useState } from "react";
import { FaTachometerAlt } from "react-icons/fa";

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
  state: "tuning" | "done" | "cpu" | "off" | "wait";
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
  lsfg: boolean;
  tune: Tune | null;
  launch_option: string;
};

const getStatus = callable<[], Status>("get_status");
const setSetting = callable<[key: string, value: unknown], boolean>("set_setting");
const reprobe = callable<[], boolean>("reprobe");
const forgetAll = callable<[], boolean>("forget_all");
const retune = callable<[], boolean>("retune");
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
};

const PHASE: Record<string, string> = {
  idle: "очікування",
  warmup: "розігрів",
  probe: "вимірювання",
  locked: "стабільно",
  manual: "вручну",
};

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
        {!st.lsfg && (
          <PanelSectionRow>
            <Field
              label="lsfg-vk не знайдено"
              description="Встановіть плагін «Decky LSFG-VK» з магазину Decky і натисніть у ньому Install lsfg-vk (потрібен Lossless Scaling у Steam)."
            />
          </PanelSectionRow>
        )}
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

      <PanelSection title="Налаштування гри">
        <PanelSectionRow>
          <Field
            label="Параметр запуску"
            description={st.launch_option}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() => {
              navigator.clipboard?.writeText(st.launch_option);
              toaster.toast({ title: "AutoFPS", body: "Параметр запуску скопійовано" });
            }}
          >
            Скопіювати параметр запуску
          </ButtonItem>
        </PanelSectionRow>
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

export default definePlugin(() => ({
  name: "AutoFPS",
  titleView: <div className={staticClasses.Title}>AutoFPS</div>,
  content: <Content />,
  icon: <FaTachometerAlt />,
  onDismount() {},
}));
