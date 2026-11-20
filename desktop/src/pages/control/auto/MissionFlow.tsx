// This file was modified on 11/20/2026 with the assistance of Claude Code in VS Code
// (Claude Opus 5.5, Anthropic, 2026). Step ① Plan now shows the Hover test panel
// above the mission list.

/**
 * The Auto flow, in the order the operator works (2026-10-05, Samuel):
 * ① Plan → ② Position → ③ Flyable space → ④ Auto-correct → ⑤ Fly.
 *
 * ① also offers the Hover test above the mission list (HoverTestPanel.tsx,
 * 2026-11-20): flown instead of a mission, so it needs none chosen.
 *
 * Adapted from the Zoomaa booking flow (booking_flow_screen.dart): one screen,
 * a step bar across the top, back always allowed, forward only once the step
 * before is satisfied — and here "satisfied" is read from the agent, never
 * decided by the page:
 *
 *   ① Plan          done when a mission is chosen that the agent says is valid
 *   ② Position      done when the drone says its position has settled
 *                   (/station ready) — measured here from where it sits
 *   ③ Flyable space the green: predicted from the base station, walked, or
 *                   the default area; never changes the plan
 *   ④ Auto-correct  the agent corrects the plan on its own; done while the
 *                   corrected plan can fly, undone the moment it cannot
 *   ⑤ Fly           the session, its checks and the area first (CheckStep),
 *                   then Start mission; LOCKED here while a mission flies
 *
 * The chosen mission is remembered per machine (localStorage), a convenience
 * only: the agent re-reads and re-validates the mission when it is started.
 */

import { useEffect, useState } from "react";
import type { History, Run } from "@/App";
import { api, type MissionView, type Session, type Telemetry } from "@/lib/agent";
import { CheckStep, checkComplete } from "./CheckStep";
import { FlyStep } from "./FlyStep";
import { MissionStep } from "./plan/MissionStep";
import { StepBar, type FlowStep, type StepState } from "./StepBar";
import { PositionStep } from "./PositionStep";
import { FlightPlanCheck } from "./FlightPlanCheck";
import { HoverTestPanel } from "./HoverTestPanel";
import { useStation } from "@/components/StationStages";

const CHOSEN_KEY = "dronedeck.auto.mission";

function remembered(): string | null {
  try { return localStorage.getItem(CHOSEN_KEY); } catch { return null; }
}

function remember(id: string | null): void {
  try {
    if (id) localStorage.setItem(CHOSEN_KEY, id);
    else localStorage.removeItem(CHOSEN_KEY);
  } catch { /* a private window: the choice lasts until the window closes */ }
}

/** Where the flow opens. The app never sets it (it opens on ①, or ③ while a
 *  mission flies); the layout harness does, to measure every step. */
export type FlowStart = { step?: FlowStep; missionId?: string; view?: "edit"; full?: boolean };

export function MissionFlow({ session, run, telemetry, history, ambient, setAmbient, start, full = false, onFullScreen, heightClass }: {
  /** Whether the flow is filling the window (AutoControl's FullScreenFrame). */
  full?: boolean;
  onFullScreen?: () => void;
  /** The room maps' height — taller in full screen. */
  heightClass?: string;
  session: Session;
  run: Run;
  telemetry: Telemetry | null;
  history: History;
  ambient: string;
  setAmbient: (value: string) => void;
  start?: FlowStart;
}) {
  const flying = session.activity === "mission";
  const [chosen, setChosen] = useState<MissionView | null>(null);
  const [step, setStep] = useState<FlowStep>(flying ? 5 : start?.step ?? 1);
  // ④: the auto-corrected plan can fly (reported by FlightPlanCheck, live).
  const [flyable, setFlyable] = useState(false);
  const station = useStation(session.radio?.state === "connected" || session.session_id != null);
  const positioned = station?.connected === true && station.ready;

  // Reopen on the mission chosen last time, if it is still here.
  useEffect(() => {
    const id = session.mission?.id ?? start?.missionId ?? remembered();
    if (!id || chosen) return;
    api.mission(id).then(setChosen).catch(() => remember(null));
  }, [session.mission?.id, start?.missionId, chosen]);

  // A mission in the air pins the flow to Fly.
  useEffect(() => { if (flying) setStep(5); }, [flying]);

  const choose = (mission: MissionView) => {
    setChosen(mission);
    setFlyable(false);
    remember(mission.id);
    setStep(2);
  };

  const ready = checkComplete(session);
  const inFlight = flying ? "A mission is flying — the plan cannot change in the air." : null;
  const steps: StepState[] = [
    { step: 1, label: "Plan", done: chosen?.valid ?? false, blocked: inFlight },
    { step: 2, label: "Position", done: positioned,
      blocked: inFlight ?? (!chosen ? "Choose a mission first." : null) },
    { step: 3, label: "Flyable space", done: positioned,
      blocked: inFlight ?? (!chosen ? "Choose a mission first."
        : !positioned ? "Measure the drone's position first (②)." : null) },
    { step: 4, label: "Auto-correct", done: flyable,
      blocked: inFlight ?? (!chosen ? "Choose a mission first."
        : !chosen.valid ? "The chosen mission has problems to fix."
        : !positioned ? "Measure the drone's position first (②)." : null) },
    { step: 5, label: "Fly", done: false,
      blocked: flying ? null
        : !chosen ? "Choose a mission first."
        : !flyable ? "The auto-corrected plan cannot fly yet — see ④." : null },
  ];

  return (
    <section aria-label="Mission" className="grid content-start gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <div className="min-w-0 flex-1"><StepBar steps={steps} current={step} onSelect={setStep} /></div>
        {!full && onFullScreen && (
          <button type="button" onClick={onFullScreen} title="Plan with the whole window"
                  className="mono min-h-9 shrink-0 border border-[var(--border)] px-2.5 text-[10px] font-semibold uppercase tracking-[0.08em] text-[var(--muted)] hover:bg-[var(--surface-2)] hover:text-[var(--foreground)]">
            Full screen ⤢
          </button>
        )}
      </div>

      {step === 1 && (
        <MissionStep run={run} telemetry={telemetry} selectedId={chosen?.id ?? null} onUse={choose}
                     openEditor={start?.view === "edit" ? start.missionId : undefined} heightClass={heightClass}
                     aside={<HoverTestPanel session={session} run={run} ambient={ambient} setAmbient={setAmbient} />} />
      )}
      {step === 2 && <PositionStep session={session} run={run} onContinue={() => setStep(3)} />}
      {step === 3 && chosen && (
        <FlightPlanCheck mission={chosen} run={run} mode="space" onContinue={() => setStep(4)} />
      )}
      {step === 4 && chosen && (
        <FlightPlanCheck mission={chosen} run={run} mode="fit" onFlyable={setFlyable}
                         onContinue={() => setStep(5)} />
      )}
      {step === 5 && chosen && (flying || (ready && session.assisted)
        ? <FlyStep session={session} run={run} telemetry={telemetry} history={history} mission={chosen}
                   ambient={ambient} setAmbient={setAmbient} onPlanAnother={() => setStep(1)} heightClass={heightClass} />
        : <CheckStep session={session} run={run} mission={chosen} />)}
    </section>
  );
}
